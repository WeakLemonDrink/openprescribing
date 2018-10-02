-- Generated by generate_step_18.py

INSERT INTO dmd_product(
  dmdid,
  bnf_code,
  vpid,
  name,
  full_name,
  ema,
  pres_statcd,
  avail_restrictcd,
  product_type,
  non_availcd,
  concept_class,
  nurse_f,
  dent_f,
  prod_order_no,
  sched_1,
  sched_2,
  padm,
  fp10_mda,
  acbs,
  assort_flav,
  catcd,
  tariff_category,
  flag_imported,
  flag_broken_bulk,
  flag_non_bioequivalence,
  flag_special_containers
)
SELECT
  dmdid,
  bnf_code,
  vpid,
  name,
  full_name,
  ema,
  pres_statcd,
  avail_restrictcd,
  product_type,
  non_availcd,
  concept_class,
  nurse_f,
  dent_f,
  prod_order_no,
  sched_1,
  sched_2,
  padm,
  fp10_mda,
  acbs,
  assort_flav,
  catcd,
  tariff_category,
  flag_imported,
  flag_broken_bulk,
  flag_non_bioequivalence,
  flag_special_containers
FROM dmd_product_temp
ON CONFLICT (dmdid) DO UPDATE SET
  dmdid = EXCLUDED.dmdid,
  bnf_code = EXCLUDED.bnf_code,
  vpid = EXCLUDED.vpid,
  name = EXCLUDED.name,
  full_name = EXCLUDED.full_name,
  ema = EXCLUDED.ema,
  pres_statcd = EXCLUDED.pres_statcd,
  avail_restrictcd = EXCLUDED.avail_restrictcd,
  product_type = EXCLUDED.product_type,
  non_availcd = EXCLUDED.non_availcd,
  concept_class = EXCLUDED.concept_class,
  nurse_f = EXCLUDED.nurse_f,
  dent_f = EXCLUDED.dent_f,
  prod_order_no = EXCLUDED.prod_order_no,
  sched_1 = EXCLUDED.sched_1,
  sched_2 = EXCLUDED.sched_2,
  padm = EXCLUDED.padm,
  fp10_mda = EXCLUDED.fp10_mda,
  acbs = EXCLUDED.acbs,
  assort_flav = EXCLUDED.assort_flav,
  catcd = EXCLUDED.catcd,
  tariff_category = EXCLUDED.tariff_category,
  flag_imported = EXCLUDED.flag_imported,
  flag_broken_bulk = EXCLUDED.flag_broken_bulk,
  flag_non_bioequivalence = EXCLUDED.flag_non_bioequivalence,
  flag_special_containers = EXCLUDED.flag_special_containers
;