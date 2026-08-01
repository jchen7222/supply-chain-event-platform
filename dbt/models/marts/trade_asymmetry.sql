-- Two reports of the same physical goods, decomposed: valuation (CIF/FOB)
-- vs residual (partner attribution and everything else).
select
    json_extract_string(payload, '$.pair')  as pair,
    json_extract_string(payload, '$.year')  as year,
    cast(json_extract_string(payload, '$.importer_reported_cif_usd') as double) as importer_cif_usd,
    cast(json_extract_string(payload, '$.exporter_reported_fob_usd') as double) as exporter_fob_usd,
    cast(json_extract_string(payload, '$.gap_usd') as double)                    as gap_usd,
    cast(json_extract_string(payload, '$.gap_pct') as double)                    as gap_pct,
    cast(json_extract_string(payload, '$.valuation_component_usd') as double)    as valuation_component_usd,
    cast(json_extract_string(payload, '$.residual_attribution_usd') as double)   as residual_attribution_usd,
    json_extract_string(payload, '$.within_tolerance') as within_tolerance
from {{ ref('current_state') }}
where event_type = 'trade_mirror_observed'
order by pair, year
