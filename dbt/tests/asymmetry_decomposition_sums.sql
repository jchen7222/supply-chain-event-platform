-- conservation at international scale: explained + residual must equal the gap
select * from {{ ref('trade_asymmetry') }}
where valuation_component_usd is not null
  and abs(valuation_component_usd + residual_attribution_usd - gap_usd) > 1.0
