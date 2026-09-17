-- Reconciliation in the other direction: parcels the courier is scanning that
-- no order in the ledger accounts for.
--
-- Every conservation check in this platform asks "did everything we took in
-- come out?". This asks the opposite and less comfortable question: is anything
-- coming out that we never took in? A waybill with scans and no order means
-- one of three things, and all of them are worth a minute of somebody's time:
--
--   * an order was taken outside the system — a WeChat message never entered
--   * the waybill was mistyped when the order was created
--   * somebody else's parcel is on our account
--
-- An empty result is the expected state. A non-empty one is a work list, not
-- an error, which is why this is a model and not a test that fails the build.
with scans as (
    select
        json_extract_string(payload, '$.waybill') as waybill,
        json_extract_string(payload, '$.status')  as status,
        json_extract_string(payload, '$.carrier') as carrier,
        event_time                                 as scanned_on
    from {{ ref('stg_events') }}
    where event_type = 'shipment_scanned' and not is_error_declaration
),
ordered_waybills as (
    select distinct json_extract_string(payload, '$.waybill') as waybill
    from {{ ref('stg_events') }}
    where event_type = 'order_placed'
      and json_extract_string(payload, '$.waybill') is not null
)
select
    s.waybill,
    any_value(s.carrier)  as carrier,
    count(*)              as scan_count,
    min(s.scanned_on)     as first_scanned_on,
    max(s.scanned_on)     as last_scanned_on,
    arg_max(s.status, s.scanned_on) as last_status
from scans s
where s.waybill not in (select waybill from ordered_waybills)
group by s.waybill
order by s.waybill
