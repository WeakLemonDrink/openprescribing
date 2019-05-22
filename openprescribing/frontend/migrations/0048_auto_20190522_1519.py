# -*- coding: utf-8 -*-
# Generated by Django 1.11.20 on 2019-05-22 14:19
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('frontend', '0047_auto_20190514_1413'),
    ]

    operations = [
        migrations.RunSQL('DROP MATERIALIZED VIEW vw__medians_for_tariff'),
        migrations.RunSQL("""
        CREATE MATERIALIZED VIEW vw__medians_for_tariff AS (
            WITH
                recent_date AS (
                    SELECT MAX(current_at) AS current_at
                    FROM frontend_importlog
                    WHERE category = 'prescribing'
                ),

                vmps_with_one_ppu AS (
                    SELECT vmp.vpid 
                    FROM dmd2_vmp vmp
                    INNER JOIN dmd2_vmpp vmpp ON vmp.vpid = vmpp.vpid
                    INNER JOIN frontend_tariffprice tp ON vmpp.vppid = tp.vmpp_id
                    WHERE tp.date IN (SELECT current_at FROM recent_date)
                    GROUP BY vmp.vpid
                    HAVING stddev_pop(tp.price_pence / vmpp.qtyval) = 0
                )

            SELECT
                vmp.vpid,
                vmp.nm,
                vmp.bnf_code,
                rx.processing_date AS date,
                percentile_disc(0.5::double precision) WITHIN GROUP (
                    ORDER BY (
                        CASE
                            WHEN rx.quantity > 0::double precision
                                THEN rx.net_cost / rx.quantity
                            ELSE 0::double precision
                        END
                    )
                ) AS median_ppu

            FROM dmd2_vmp vmp
            INNER JOIN frontend_prescription rx
                ON vmp.bnf_code = rx.presentation_code

            WHERE
                vmp.vpid IN (SELECT vpid FROM vmps_with_one_ppu)
                AND rx.processing_date IN (SELECT current_at FROM recent_date)

            GROUP BY
                vmp.vpid,
                rx.processing_date
        )
      """)
    ]
