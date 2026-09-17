-- THE SPINE: one row per customer order, from the quote to the last scan.
--
-- Four lanes meet here and none of them can produce this row alone:
--
--   order_intake      what the customer asked for, and what we quoted —
--                     at the FX rate in force on the order date
--   usitc_hts         the duty rate in force on that same date
--   courier_tracking  where the parcel actually got to
--   (dispatch_planner routes a separate synthetic population — see
--    landed_duty_by_order; the two order id spaces are deliberately not
--    joined, because inventing a mapping would be a lie)
--
-- WHAT THIS MODEL FOUND. The pricing tool computes landed cost as retail plus
-- sales tax plus freight plus handling. It does not know about duty, because
-- nothing in the spreadsheet world did. Apparel duty is 28.2% on knitted
-- synthetic trousers and 32% on man-made-fibre pullovers — so `duty_cad` is
-- usually the largest single line in the landed cost, and `true_profit_cad`
-- is the number that decides whether the order was worth taking.
--
-- `quoted_profit_cad` is what the tool believed. `true_profit_cad` is what the
-- ledger can prove. The gap between them is the point of building this.
with placed as (
    select
        entity_id,
        json_extract_string(payload, '$.order_ref')     as order_ref,
        json_extract_string(payload, '$.customer')      as customer,
        json_extract_string(payload, '$.platform')      as platform,
        json_extract_string(payload, '$.product_name')  as product_name,
        json_extract_string(payload, '$.colour')        as colour,
        json_extract_string(payload, '$.size')          as size,
        cast(json_extract_string(payload, '$.quantity') as integer)         as quantity,
        cast(json_extract_string(payload, '$.retail_price_cad') as double)  as retail_price_cad,
        json_extract_string(payload, '$.hts_code')      as hts_code,
        json_extract_string(payload, '$.waybill')       as waybill,
        event_time                                       as ordered_on
    from {{ ref('stg_events') }}
    where event_type = 'order_placed' and not is_error_declaration
),
quoted as (
    select
        entity_id,
        cast(json_extract_string(payload, '$.sell_cny') as double)   as sell_cny,
        cast(json_extract_string(payload, '$.total_cny') as double)  as total_cny,
        cast(json_extract_string(payload, '$.landed_cad') as double) as quoted_landed_cad,
        cast(json_extract_string(payload, '$.profit_cad') as double) as quoted_profit_cad,
        cast(json_extract_string(payload, '$.fx_rate') as double)    as fx_rate,
        json_extract_string(payload, '$.fx_effective_from')          as fx_effective_from,
        json_extract_string(payload, '$.pricing_version')            as pricing_version,
        json_extract_string(payload, '$.capped_by_china_price') = 'true' as capped_by_china_price
    from {{ ref('stg_events') }}
    where event_type = 'order_quoted' and not is_error_declaration
    qualify row_number() over (
        partition by entity_id order by record_time desc, seq desc) = 1
),
rejected as (
    select entity_id,
           json_extract_string(payload, '$.reason') as rejection_reason
    from {{ ref('stg_events') }}
    where event_type = 'order_rejected' and not is_error_declaration
    qualify row_number() over (
        partition by entity_id order by record_time desc, seq desc) = 1
),
-- Full revision chain, not a fold: the join below needs the rate in force on
-- each order's own date, so collapsing to the latest revision would be wrong.
rates as (
    select
        json_extract_string(payload, '$.hts_code')        as hts_code,
        json_extract_string(payload, '$.general_rate_raw') as duty_rate_raw,
        cast(json_extract_string(payload, '$.ad_valorem_rate') as double) as duty_rate,
        json_extract_string(payload, '$.rate_kind')       as duty_rate_kind,
        event_time                                        as duty_effective_from
    from {{ ref('stg_events') }}
    where event_type = 'duty_rate_observed' and not is_error_declaration
),
scans as (
    select
        json_extract_string(payload, '$.waybill')  as waybill,
        json_extract_string(payload, '$.status')   as status,
        json_extract_string(payload, '$.site')     as last_site,
        json_extract_string(payload, '$.carrier')  as carrier,
        event_time                                  as scanned_on
    from {{ ref('stg_events') }}
    where event_type = 'shipment_scanned' and not is_error_declaration
),
last_scan as (
    -- Latest by when the scan HAPPENED, never by when it arrived: a delivery
    -- webhook that wins the race would otherwise outrank a later customs hold.
    select * from scans
    qualify row_number() over (
        partition by waybill order by scanned_on desc) = 1
),
scan_counts as (
    select waybill, count(*) as scan_count, min(scanned_on) as first_scanned_on
    from scans group by 1
),
with_duty as (
    select
        p.*, q.*, r.rejection_reason,
        d.duty_rate_raw, d.duty_rate, d.duty_rate_kind, d.duty_effective_from,
        row_number() over (
            partition by p.entity_id
            order by d.duty_effective_from desc nulls last) as rn
    from placed p
    left join quoted   q on q.entity_id = p.entity_id
    left join rejected r on r.entity_id = p.entity_id
    left join rates    d on d.hts_code = p.hts_code
                        and d.duty_effective_from <= p.ordered_on
)
select
    w.order_ref,
    w.customer,
    w.platform,
    w.product_name,
    w.colour,
    w.size,
    w.quantity,
    w.ordered_on,

    -- what we quoted, and what produced it
    w.sell_cny,
    w.total_cny,
    w.fx_rate,
    w.fx_effective_from,
    w.pricing_version,
    w.capped_by_china_price,
    w.quoted_landed_cad,
    w.quoted_profit_cad,

    -- the duty the quote never knew about
    w.hts_code,
    w.duty_rate_raw,
    w.duty_rate,
    w.duty_rate_kind,
    w.duty_effective_from,
    case when w.duty_rate is not null
         then round(w.retail_price_cad * w.duty_rate * w.quantity, 2) end as duty_cad,
    case when w.duty_rate is not null and w.quoted_profit_cad is not null
         then round(w.quoted_profit_cad
                    - w.retail_price_cad * w.duty_rate * w.quantity, 2) end
        as true_profit_cad,

    -- where it got to
    w.waybill,
    s.carrier,
    s.status            as last_status,
    s.last_site,
    s.scanned_on        as last_scanned_on,
    c.scan_count,

    -- one column to read the row by
    case
        when w.rejection_reason is not null            then 'rejected'
        when w.waybill is null                         then 'quoted_not_shipped'
        when s.status = 'delivered'                    then 'delivered'
        when s.status = 'returned'                     then 'returned'
        when s.status in ('customs_held', 'exception')  then 'needs_attention'
        when s.status is null                          then 'no_scans_yet'
        else 'in_flight'
    end as order_state,
    w.rejection_reason
from with_duty w
left join last_scan  s on s.waybill = w.waybill
left join scan_counts c on c.waybill = w.waybill
where w.rn = 1
order by w.order_ref
