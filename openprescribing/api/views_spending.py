from sets import Set
import datetime
import re

import numpy as np

from django.db import connection
from django.db.models import Q
from django.forms.models import model_to_dict
from django.shortcuts import get_object_or_404

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.exceptions import APIException

from common.utils import namedtuplefetchall, nhs_titlecase
from dmd.models import DMDProduct, DMDVmpp, NCSOConcession
from frontend.models import GenericCodeMapping
from frontend.models import ImportLog
from frontend.models import PPUSaving
from frontend.models import Presentation
from frontend.models import Practice, PCT
import view_utils as utils
from view_utils import db_timeout


CODE_LENGTH_ERROR = (
    'Error: BNF Codes must all be the same length if written in the same '
    'search box. For example, you cannot search for Cerazette_Tab 75mcg '
    '(0703021Q0BBAAAA) and Cerelle (0703021Q0BD), but you could search for '
    'Cerazette (0703021Q0BB) and Cerelle (0703021Q0BD). If you need this '
    'data, please <a href="mailto:{{ SUPPORT_EMAIL }}" '
    'class="doorbell-show">get in touch</a> and we may be able to extract it '
    'for you')


class NotValid(APIException):
    status_code = 400
    default_detail = 'The code you provided is not valid'


def _valid_or_latest_date(date):
    if date:
        try:
            date = datetime.datetime.strptime(date, '%Y-%m-%d')
        except ValueError:
            raise NotValid("%s is not a valid date" % date)
    else:
        date = ImportLog.objects.latest_in_category('prescribing').current_at
    return date


def _build_conditions_and_patterns(code, focus):
    if not re.match(r'[A-Z0-9]{15}', code):
        raise NotValid("%s is not a valid code" % code)
    extra_codes = GenericCodeMapping.objects.filter(
        Q(from_code=code) | Q(to_code=code))
    # flatten and uniquify the list of codes
    extra_codes = Set(np.array(
        [[x.from_code, x.to_code] for x in extra_codes]).flatten())
    patterns = ["%s____%s" % (code[:9], code[13:15])]
    for extra_code in extra_codes:
        if extra_code.endswith('%'):
            pattern = extra_code
        else:
            pattern = "%s____%s" % (extra_code[:9], extra_code[13:15])
        patterns.append(pattern)
    conditions = " OR ".join(["presentation_code LIKE %s "] * len(patterns))
    conditions = "AND (%s) " % conditions
    if focus:
        if len(focus) == 3:
            conditions += "AND (pct_id = %s)"
        else:
            conditions += "AND (practice_id = %s)"
        patterns.append(focus)
    return conditions, patterns


@api_view(['GET'])
def bubble(request, format=None):
    """Returns data relating to price-per-unit, in a format suitable for
    use in Highcharts bubble chart.

    """
    code = request.query_params.get('bnf_code', '')
    trim = request.query_params.get('trim', '')
    date = _valid_or_latest_date(request.query_params.get('date', None))
    highlight = request.query_params.get('highlight', None)
    focus = request.query_params.get('focus', None) and highlight
    conditions, patterns = _build_conditions_and_patterns(code, focus)
    rounded_ppus_cte_sql = (
        "WITH rounded_ppus AS (SELECT presentation_code, "
        "COALESCE(frontend_presentation.name, 'unknown') "
        "AS presentation_name, "
        "quantity, net_cost, practice_id, pct_id, "
        "ROUND(CAST(net_cost/NULLIF(quantity, 0) AS numeric), 2) AS ppu "
        "FROM frontend_prescription "
        "LEFT JOIN frontend_presentation "
        "ON frontend_prescription.presentation_code = "
        "frontend_presentation.bnf_code "
        "LEFT JOIN frontend_practice ON frontend_practice.code = practice_id "
        "WHERE processing_date = %s "
        "AND setting = 4 " +
        conditions +
        ") "
    )
    binned_ppus_sql = rounded_ppus_cte_sql + (
        ", binned_ppus AS (SELECT presentation_code, presentation_name, ppu, "
        "SUM(quantity) AS quantity "
        "FROM rounded_ppus "
        "GROUP BY presentation_code, presentation_name, ppu) "
    )
    if trim:
        # Skip items where PPU is outside <trim> percentile (where
        # <trim> is out of 100)
        trim = float(trim)
        out_of = 100
        while trim % 1 != 0:
            trim = trim * 10
            out_of = out_of * 10
        ordered_ppus_sql = binned_ppus_sql + (
            "SELECT * FROM ("
            " SELECT *, "
            " AVG(ppu) OVER ("
            "  PARTITION BY presentation_code) AS mean_ppu, "
            " NTILE(%s) OVER (ORDER BY ppu) AS ntiled "
            " FROM binned_ppus "
            " ORDER BY mean_ppu, presentation_name) ranked "
            "WHERE ntiled <= %s" % (out_of, trim)
        )
    else:

        ordered_ppus_sql = binned_ppus_sql + (
            "SELECT *, "
            "AVG(ppu) OVER (PARTITION BY presentation_code) AS mean_ppu "
            "FROM binned_ppus "
            "ORDER BY mean_ppu, presentation_name, ppu"
        )
    mean_ppu_for_entity_sql = rounded_ppus_cte_sql + (
        "SELECT SUM(net_cost)/SUM(quantity) FROM rounded_ppus "
    )
    params = [date] + patterns
    with connection.cursor() as cursor:
        cursor.execute(ordered_ppus_sql, params)
        series = []
        categories = []
        pos = 0
        for result in namedtuplefetchall(cursor):
            if result.presentation_name not in [x['name'] for x in categories]:
                pos += 1
                is_generic = False
                if result.presentation_code[9:11] == 'AA':
                    is_generic = True
                categories.append(
                    {
                        'name': result.presentation_name,
                        'is_generic': is_generic
                    }
                )

            series.append({
                'x': pos,
                'y': result.ppu,
                'z': result.quantity,
                'mean_ppu': result.mean_ppu,
                'name': result.presentation_name})
        if highlight:
            params.append(highlight)
            if len(highlight) == 3:
                mean_ppu_for_entity_sql += "WHERE pct_id = %s "
            else:
                mean_ppu_for_entity_sql += "WHERE practice_id = %s "
        cursor.execute(mean_ppu_for_entity_sql, params)
        plotline = cursor.fetchone()[0]
        return Response(
            {'plotline': plotline, 'series': series, 'categories': categories})


@api_view(['GET'])
def price_per_unit(request, format=None):
    """Returns price per unit data for presentations and practices or
    CCGs

    """
    entity_code = request.query_params.get('entity_code')
    date = request.query_params.get('date')
    bnf_code = request.query_params.get('bnf_code')
    if not date:
        raise NotValid("You must supply a date")
    if not (entity_code or bnf_code):
        raise NotValid("You must supply a value for entity_code or bnf_code")

    params = {'date': date}
    filename = date

    practice_level = False

    if bnf_code:
        params['bnf_code'] = bnf_code
        get_object_or_404(Presentation, pk=bnf_code)
        filename += "-%s" % bnf_code

    if entity_code:
        params['entity_code'] = entity_code
        if len(entity_code) == 3:
            get_object_or_404(PCT, pk=entity_code)
        else:
            get_object_or_404(Practice, pk=entity_code)
            practice_level = True
        filename += "-%s" % entity_code

    # We cannot use the ORM here since there is no ForeignKey from PPUSaving to
    # DMDProduct.
    sql = '''
    SELECT DISTINCT ON (
            {dmdproduct_table}.bnf_code,
            {ppusavings_table}.pct_id,
            {ppusavings_table}.practice_id
        )
        {ppusavings_table}.id AS id,
        {ppusavings_table}.date AS date,
        {ppusavings_table}.lowest_decile AS lowest_decile,
        {ppusavings_table}.quantity AS quantity,
        {ppusavings_table}.price_per_unit AS price_per_unit,
        {ppusavings_table}.possible_savings AS possible_savings,
        {ppusavings_table}.formulation_swap AS formulation_swap,
        {ppusavings_table}.pct_id AS pct,
        {ppusavings_table}.practice_id AS practice,
        {ppusavings_table}.bnf_code AS presentation,
        {practice_table}.name AS practice_name,
        {dmdproduct_table}.flag_non_bioequivalence AS flag_bioequivalence,
        subquery.price_concession IS NOT NULL as price_concession,
        COALESCE({dmdproduct_table}.name, {presentation_table}.name) AS name
    FROM {ppusavings_table}
    LEFT OUTER JOIN {presentation_table}
        ON {ppusavings_table}.bnf_code = {presentation_table}.bnf_code
    LEFT OUTER JOIN {practice_table}
        ON {ppusavings_table}.practice_id = {practice_table}.code
    LEFT OUTER JOIN {dmdproduct_table}
        ON {ppusavings_table}.bnf_code = {dmdproduct_table}.bnf_code
    LEFT OUTER JOIN (SELECT DISTINCT vpid, 1 AS price_concession
                     FROM {dmdvmpp_table}
                     INNER JOIN {ncsoconcession_table}
                         ON {dmdvmpp_table}.vppid = {ncsoconcession_table}.vmpp_id
                     WHERE {ncsoconcession_table}.date = %(date)s) AS subquery
        ON {dmdproduct_table}.vpid = subquery.vpid
    WHERE
        {ppusavings_table}.date = %(date)s
        AND {dmdproduct_table}.concept_class = 1'''

    if bnf_code:
        sql += '''
        AND {ppusavings_table}.bnf_code = %(bnf_code)s'''

    if entity_code:
        if practice_level:
            sql += '''
        AND {ppusavings_table}.practice_id = %(entity_code)s'''
        else:
            sql += '''
        AND {ppusavings_table}.pct_id = %(entity_code)s'''

            if bnf_code:
                sql += '''
        AND {ppusavings_table}.practice_id IS NOT NULL'''
            else:
                sql += '''
        AND {ppusavings_table}.practice_id IS NULL'''

    sql = sql.format(
        ppusavings_table=PPUSaving._meta.db_table,
        practice_table=Practice._meta.db_table,
        presentation_table=Presentation._meta.db_table,
        dmdproduct_table=DMDProduct._meta.db_table,
        dmdvmpp_table=DMDVmpp._meta.db_table,
        ncsoconcession_table=NCSOConcession._meta.db_table,
    )

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        results = utils.dictfetchall(cursor)

    for result in results:
        if result['practice_name'] is not None:
            result['practice_name'] = nhs_titlecase(result['practice_name'])

    response = Response(results)
    if request.accepted_renderer.format == 'csv':
        filename = "%s-ppd.csv" % (filename)
        response['content-disposition'] = "attachment; filename=%s" % filename
    return response


@db_timeout(58000)
@api_view(['GET'])
def total_spending(request, format=None):
    codes = utils.param_to_list(request.query_params.get('code', []))
    codes = utils.get_bnf_codes_from_number_str(codes)

    spending_type = utils.get_spending_type(codes)
    if spending_type is False:
        err = CODE_LENGTH_ERROR
        return Response(err, status=400)

    query = _get_query_for_total_spending(codes)

    if spending_type != 'presentation':
        codes = [c + '%' for c in codes]

    data = utils.execute_query(query, [codes])
    return Response(data)


@api_view(['GET'])
def tariff(request, format=None):
    # This view uses raw SQL as we cannot produce the LEFT OUTER JOIN using the
    # ORM.
    codes = utils.param_to_list(request.query_params.get('codes', []))

    query = '''
    SELECT dmd_tariffprice.date AS date,
           dmd_tariffprice.price_pence AS price_pence,
           dmd_vmpp.nm AS vmpp,
           dmd_product.bnf_code AS product,
           dmd_ncsoconcession.price_concession_pence AS concession,
           dmd_lookup_dt_payment_category.desc AS tariff_category
    FROM dmd_tariffprice
        INNER JOIN dmd_lookup_dt_payment_category
            ON dmd_tariffprice.tariff_category_id = dmd_lookup_dt_payment_category.cd
        INNER JOIN dmd_product
            ON dmd_tariffprice.product_id = dmd_product.dmdid
        INNER JOIN dmd_vmpp
            ON dmd_tariffprice.vmpp_id = dmd_vmpp.vppid
        LEFT OUTER JOIN dmd_ncsoconcession
            ON (dmd_tariffprice.date = dmd_ncsoconcession.date
                AND dmd_tariffprice.vmpp_id = dmd_ncsoconcession.vmpp_id)
    '''

    if codes:
        query += ' WHERE dmd_product.bnf_code IN ('
        query += ','.join('%s' for _ in range(len(codes)))
        query += ')'
        params = [codes]
    else:
        params = None

    query += ' ORDER BY date'

    data = utils.execute_query(query, params)
    response = Response(data)
    if request.accepted_renderer.format == 'csv':
        filename = "tariff.csv"
        response['content-disposition'] = "attachment; filename=%s" % filename
    return response


@db_timeout(58000)
@api_view(['GET'])
def spending_by_ccg(request, format=None):
    codes = utils.param_to_list(request.query_params.get('code', []))
    codes = utils.get_bnf_codes_from_number_str(codes)
    orgs = utils.param_to_list(request.query_params.get('org', []))

    spending_type = utils.get_spending_type(codes)
    if spending_type is False:
        err = CODE_LENGTH_ERROR
        return Response(err, status=400)

    if not spending_type or spending_type == 'bnf-section' \
       or spending_type == 'chemical':
        query = _get_query_for_chemicals_or_sections_by_ccg(codes, orgs,
                                                            spending_type)
    else:
        query = _get_query_for_presentations_by_ccg(codes, orgs)

    if spending_type == 'bnf-section' or spending_type == 'product':
        codes = [c + '%' for c in codes]

    data = utils.execute_query(query, [codes, orgs])
    return Response(data)


@db_timeout(58000)
@api_view(['GET'])
def spending_by_practice(request, format=None):
    codes = utils.param_to_list(request.query_params.get('code', []))
    codes = utils.get_bnf_codes_from_number_str(codes)
    org_ids = utils.param_to_list(request.query_params.get('org', []))
    date = request.query_params.get('date', None)

    spending_type = utils.get_spending_type(codes)
    if spending_type is False:
        err = 'Error: Codes must all be the same length'
        return Response(err, status=400)

    if spending_type == 'bnf-section' or spending_type == 'product':
        codes = [c + '%' for c in codes]

    if not date and not org_ids:
        err = 'Error: You must supply either '
        err += 'a list of practice IDs or a date parameter, e.g. '
        err += 'date=2015-04-01'
        return Response(err, status=400)

    params = [codes]

    if spending_type in ['product', 'presentation']:
        assert len(codes) > 0
        query = _get_presentations_by_practice(codes, org_ids, date)
        params.append(org_ids)

    elif spending_type in ['bnf-section', 'chemical']:
        assert len(codes) > 0
        practice_ids = utils.get_practice_ids_from_org(org_ids)
        query = _get_chemicals_or_sections_by_practice(codes,
                                                       practice_ids,
                                                       spending_type,
                                                       date)
        params.append(practice_ids)

    else:
        assert spending_type is None
        assert len(codes) == 0

        practice_ids = utils.get_practice_ids_from_org(org_ids)
        query = _get_total_spending_by_practice(practice_ids, date)
        params.append(practice_ids)

    if date:
        params.append([date])

    data = utils.execute_query(query, params)
    return Response(data)


def _get_query_for_total_spending(codes):
    # The CTE at the start ensures we return rows for every month in
    # the last five years, even if that's zeros
    query = """WITH all_dates AS (
                 SELECT
                   MAX(current_at)::date - (d.date||'month')::interval AS date
                 FROM
                   generate_series(0,
                     59) AS d(date),
                   frontend_importlog
                 WHERE
                   category = 'prescribing'
                 GROUP BY
                   category,
                   d.date
                 ORDER BY
                   date)
               SELECT
                 COALESCE(SUM(cost), 0) AS actual_cost,
                 COALESCE(SUM(items), 0) AS items,
                 COALESCE(SUM(quantity), 0) AS quantity,
                 all_dates.date::date AS date
               FROM (
                 SELECT *
                 FROM
                   vw__presentation_summary
                 %s
               ) pr
               RIGHT OUTER JOIN all_dates
               ON all_dates.date = pr.processing_date
               GROUP BY date
               ORDER BY date;"""
    if codes:
        condition = " WHERE ("
        for i, c in enumerate(codes):
            condition += "presentation_code LIKE %s "
            if (i != len(codes) - 1):
                condition += ' OR '
        condition += ") "
    else:
        condition = ""

    return query % condition


def _get_query_for_chemicals_or_sections_by_ccg(codes, orgs, spending_type):
    query = 'SELECT pc.code as row_id, '
    query += "pc.name as row_name, "
    query += 'pr.processing_date as date, '
    query += 'SUM(pr.cost) AS actual_cost, '
    query += 'SUM(pr.items) AS items, '
    query += 'SUM(pr.quantity) AS quantity '
    query += "FROM vw__chemical_summary_by_ccg pr "
    query += "JOIN frontend_pct pc ON pr.pct_id=pc.code "
    query += "AND pc.org_type='CCG' "
    if spending_type:
        query += " WHERE ("
        if spending_type == 'bnf-section':
            for i, c in enumerate(codes):
                query += "pr.chemical_id LIKE %s "
                if (i != len(codes) - 1):
                    query += ' OR '
            codes = [c + '%' for c in codes]
        else:
            for i, c in enumerate(codes):
                query += "pr.chemical_id=%s "
                if (i != len(codes) - 1):
                    query += ' OR '
        query += ") "
    if orgs:
        query += "AND ("
        for i, org in enumerate(orgs):
            query += "pr.pct_id=%s "
            if (i != len(orgs) - 1):
                query += ' OR '
        query += ") "
    query += "GROUP BY pc.code, pc.name, date "
    query += "ORDER BY date, pc.code "
    return query


def _get_query_for_presentations_by_ccg(codes, orgs):
    query = 'SELECT pc.code as row_id, '
    query += "pc.name as row_name, "
    query += 'pr.processing_date as date, '
    query += "SUM(pr.items) AS items, "
    query += 'SUM(pr.cost) AS actual_cost, '
    query += 'SUM(pr.quantity) AS quantity '
    query += "FROM vw__presentation_summary_by_ccg pr "
    query += "JOIN frontend_pct pc ON pr.pct_id=pc.code "
    query += "AND pc.org_type='CCG' "
    query += " WHERE ("
    for i, c in enumerate(codes):
        query += "pr.presentation_code LIKE %s "
        if (i != len(codes) - 1):
            query += ' OR '
    if orgs:
        query += ") AND ("
        for i, org in enumerate(orgs):
            query += "pr.pct_id=%s "
            if (i != len(orgs) - 1):
                query += ' OR '
    query += ") GROUP BY pc.code, pc.name, date "
    query += "ORDER BY date, pc.code"
    return query


def _get_total_spending_by_practice(practice_ids, date):
    query = '''
    SELECT
        pr.practice_id AS row_id,
        pc.name AS row_name,
        pc.setting AS setting,
        pc.ccg_id AS ccg,
        pr.processing_date AS date,
        pr.cost AS actual_cost,
        pr.items AS items,
        pr.quantity AS quantity
    FROM vw__practice_summary pr
    JOIN frontend_practice pc ON pr.practice_id=pc.code
    WHERE %s
    ORDER BY date, pr.practice_id
    '''

    if practice_ids:
        practice_clauses = [
            'pr.practice_id = %s'
            for _ in range(len(practice_ids))
        ]
    else:
        practice_clauses = None

    if date:
        date_clause = 'pr.processing_date = %s'
    else:
        date_clause = None

    where_condition = _build_where_condition([
        practice_clauses,
        date_clause,
    ])

    return query % where_condition


def _get_chemicals_or_sections_by_practice(codes, practice_ids, spending_type,
                                           date):
    query = '''
    SELECT
        pc.code AS row_id,
        pc.name AS row_name,
        pc.setting AS setting,
        pc.ccg_id AS ccg,
        pr.processing_date AS date,
        SUM(pr.cost) AS actual_cost,
        SUM(pr.items) AS items,
        SUM(pr.quantity) AS quantity
    FROM vw__chemical_summary_by_practice pr
    JOIN frontend_practice pc ON pr.practice_id=pc.code
    WHERE %s
    GROUP BY pc.code, pc.name, date
    ORDER BY date, pc.code
    '''

    if spending_type == 'bnf-section':
        chemical_clauses = [
            'pr.chemical_id LIKE %s'
            for _ in range(len(codes))
        ]
    elif spending_type == 'chemical':
        chemical_clauses = [
            'pr.chemical_id = %s'
            for _ in range(len(codes))
        ]
    else:
        assert False

    if practice_ids:
        practice_clauses = [
            'pr.practice_id = %s'
            for _ in range(len(practice_ids))
        ]
    else:
        practice_clauses = None

    if date:
        date_clause = 'pr.processing_date = %s'
    else:
        date_clause = None

    where_condition = _build_where_condition([
        chemical_clauses,
        practice_clauses,
        date_clause,
    ])

    return query % where_condition


def _get_presentations_by_practice(codes, org_ids, date):
    query = '''
    SELECT
        pc.code AS row_id,
        pc.name AS row_name,
        pc.setting AS setting,
        pc.ccg_id AS ccg,
        pr.processing_date AS date,
        SUM(pr.actual_cost) AS actual_cost,
        SUM(pr.total_items) AS items,
        CAST(SUM(pr.quantity) AS bigint) AS quantity
    FROM frontend_prescription pr
    JOIN frontend_practice pc ON pr.practice_id=pc.code
    WHERE %s
    GROUP BY pc.code, pc.name, date
    ORDER BY date, pc.code
    '''

    code_subclauses = [
        'pr.presentation_code LIKE %s'
        for _ in range(len(codes))
    ]

    org_subclauses = []
    if org_ids:
        for org_id in org_ids:
            if len(org_id) == 3:
                org_subclauses.append('pr.pct_id = %s')
            else:
                org_subclauses.append('pr.practice_id = %s')

    if date:
        date_clause = 'pr.processing_date = %s'
    else:
        date_clause = None

    where_condition = _build_where_condition([
        code_subclauses,
        org_subclauses,
        date_clause,
    ])

    return query % where_condition


def _build_where_condition(clauses):
    fragments = []

    for clause in clauses:
        if clause is None:
            continue
        elif isinstance(clause, str):
            fragments.append(clause)
        elif isinstance(clause, list):
            if len(clause) == 0:
                continue
            elif len(clause) == 1:
                fragments.append(clause[0])
            else:
                fragment = '(' + ' OR '.join(clause) + ')'
                fragments.append(fragment)
        else:
            assert False, 'Unexpected clause: {}'.format(clause)

    return ' AND '.join(fragments)
