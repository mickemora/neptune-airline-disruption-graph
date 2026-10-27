-- Demo queries for the Databricks SQL editor, in the order of the walkthrough.
-- Replace `workspace` if you deployed to another catalog (bundle variable `catalog`).

-- 1. Several source systems, one day: what landed in bronze vs what survived silver.
SELECT 'schedule rows' AS what, (SELECT count(*) FROM workspace.airline_bronze.schedule) AS bronze,
       (SELECT count(*) FROM workspace.airline_silver.flights) AS silver
UNION ALL
SELECT 'reservation changes / confirmed segments',
       (SELECT count(*) FROM workspace.airline_bronze.reservations_cdc),
       (SELECT count(*) FROM workspace.airline_silver.bookings)
UNION ALL
SELECT 'loyalty records / passengers',
       (SELECT count(*) FROM workspace.airline_bronze.loyalty_members),
       (SELECT count(*) FROM workspace.airline_silver.passengers)
UNION ALL
SELECT 'fleet master / active aircraft',
       (SELECT count(*) FROM workspace.airline_bronze.aircraft_master),
       (SELECT count(*) FROM workspace.airline_silver.aircraft);

-- 2. The reservations CDC feed: inserts, updates (moves, cancels) and deletes.
SELECT OP, SEG_STATUS, count(*) AS changes
FROM workspace.airline_bronze.reservations_cdc
GROUP BY ALL ORDER BY OP, SEG_STATUS;

-- 3. The governed business rules every layer reads (lakehouse, metric views, graph).
SELECT rule_id, value, unit, owner, description
FROM workspace.airline_semantic.business_rules ORDER BY rule_id;

-- 4. The ontology, published as data: elements, and how each maps to gold and to Neptune.
SELECT kind, name, from_label, to_label, gold_table, export_file, description
FROM workspace.airline_semantic.ontology_elements ORDER BY kind DESC, name;

SELECT property, gold_column, graph_type, description
FROM workspace.airline_semantic.ontology_properties WHERE element = 'Flight';

-- 5. Metric views: KPIs defined once, queried with MEASURE().
SELECT `Connect Airport`,
       MEASURE(`Connecting Passengers`) AS connecting_pax,
       MEASURE(`Priority Passengers`)   AS priority_pax,
       MEASURE(`Tight Connections`)     AS tight,
       MEASURE(`Below Minimum Connect Time`) AS below_mct,
       MEASURE(`Minimum Connect Time`)  AS mct_rule
FROM workspace.airline_semantic.passenger_connections
GROUP BY ALL ORDER BY connecting_pax DESC LIMIT 10;

SELECT `Crew Base`,
       MEASURE(`Pairings`) AS pairings,
       MEASURE(`Tightest Duty Buffer Minutes`) AS tightest_buffer,
       MEASURE(`Pairings Within 60 Min Of Limit`) AS within_60
FROM workspace.airline_semantic.crew_duty
GROUP BY ALL ORDER BY tightest_buffer;

SELECT `Status`, MEASURE(`Flights`) AS flights, MEASURE(`Load Factor`) AS load_factor,
       MEASURE(`Average Delay Minutes`) AS avg_delay
FROM workspace.airline_semantic.flight_operations
GROUP BY ALL;

-- 6. What Neptune will load: the graph projection, e.g. flights ops control has delayed.
SELECT node_id, flight_id, origin, dest, sched_dep, status, est_delay_min, booked
FROM workspace.airline_gold.node_flight WHERE status = 'DELAYED';

-- 7. Change a governed rule (then re-run the job and reload Neptune).
-- UPDATE workspace.airline_semantic.business_rules
--   SET value = '45', updated_at = current_timestamp() WHERE rule_id = 'MIN_CONNECT_MIN';
-- After the job: query 5 shows MCT 45 and connections below the new minimum, and
-- `python scripts/graph.py impact WN4608 --delay 120` reports more misconnects.
-- Reset:
-- UPDATE workspace.airline_semantic.business_rules
--   SET value = '30', updated_at = current_timestamp() WHERE rule_id = 'MIN_CONNECT_MIN';
