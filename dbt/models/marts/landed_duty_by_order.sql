-- THE QUERY THE SEAM EXISTS FOR. Neither repository can answer this alone:
-- the planner knows which rule routed which parcel, this repository knows what
-- the tariff schedule said on any past date, and the answer needs both.
--
--   select matched_rule, count(*), round(avg(ad_valorem_rate), 4)
--   from landed_duty_by_order
--   where rule_set_version = 'v1' and pricing_status = 'priceable'
--   group by 1;
--
-- THE JOIN IS PER ROW, NOT PER BUILD. duty_rate_as_of takes one `as_of` for
-- the whole model, which is right when you are reconstructing the schedule on
-- a single date. Here every order carries its OWN ship date, so the rate has
-- to be looked up as-of each row: the latest revision effective on or before
-- that order's ship_date. A single global as_of would price a September
-- shipment and a June shipment at the same rate, which is the exact failure
-- the effective-dating exists to prevent.
--
-- Decisions come from point_in_time (one current decision per order, as-of
-- aware). Rates come from stg_events rather than a fold, because the fold
-- collapses each heading to its latest revision and this join needs the whole
-- chain to pick the one in force on a past date.
with decisions as (
    select
        entity_id,
        json_extract_string(payload, '$.commodity_class')  as commodity_class,
        json_extract_string(payload, '$.hts_code')         as hts_code,
        cast(json_extract_string(payload, '$.weight_kg') as double) as weight_kg,
        json_extract_string(payload, '$.rule_set_version') as rule_set_version,
        json_extract_string(payload, '$.matched_rule')     as matched_rule,
        json_extract_string(payload, '$.ship_date')        as ship_date,
        -- json_array_length, not len(): len() on a JSON value measures its
        -- TEXT, so an empty array '[]' comes back as 2 and every refused order
        -- silently reads as eligible. Caught by test_a_refused_order_gets_no_
        -- heading_and_no_rate.
        json_array_length(json_extract(payload, '$.eligible_services')) as eligible_count
    from {{ ref('point_in_time') }}
    where event_type = 'routing_decided'
),
rates as (
    select
        json_extract_string(payload, '$.hts_code')          as hts_code,
        json_extract_string(payload, '$.general_rate_raw')  as general_rate_raw,
        cast(json_extract_string(payload, '$.ad_valorem_rate') as double) as ad_valorem_rate,
        json_extract_string(payload, '$.rate_kind')         as rate_kind,
        event_time                                          as effective_from
    from {{ ref('stg_events') }}
    where event_type = 'duty_rate_observed'
      and not is_error_declaration
),
joined as (
    select
        d.*,
        r.general_rate_raw, r.ad_valorem_rate, r.rate_kind,
        r.effective_from as rate_effective_from,
        row_number() over (
            partition by d.entity_id
            order by r.effective_from desc nulls last) as rn
    from decisions d
    left join rates r
        on  r.hts_code = d.hts_code
        and r.effective_from <= d.ship_date
)
select
    replace(entity_id, 'ORDER:', '') as order_id,
    commodity_class,
    hts_code,
    weight_kg,
    rule_set_version,
    matched_rule,
    ship_date,
    general_rate_raw,
    ad_valorem_rate,
    rate_kind,
    rate_effective_from,
    -- Four outcomes, kept apart on purpose. 'no_heading' is correct behaviour
    -- for something the planner refused; the other two are gaps that must be
    -- visible rather than silently priced at zero.
    case
        when hts_code is null              then 'no_heading'
        when rate_effective_from is null   then 'no_rate_on_ship_date'
        when ad_valorem_rate is null       then 'not_ad_valorem'
        else 'priceable'
    end as pricing_status,
    case when eligible_count = 0 then true else false end as refused
from joined
where rn = 1
order by order_id
