import csv
from collections import Counter, defaultdict

from datagen.airports import AIRPORTS, HUB
from datagen.generate import CREW_DEBRIEF, MAX_CONNECT, MIN_CONNECT, MIN_TURN, Generator


def test_deterministic():
    a, b = Generator(seed=7).run(), Generator(seed=7).run()
    assert [f.flight_id for f in a.flights] == [f.flight_id for f in b.flights]
    assert len(a.bookings) == len(b.bookings)


def test_deterministic_across_processes(tmp_path):
    """Same seed must give the same schedule in a fresh interpreter (hash seed differs)."""
    import os
    import subprocess
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    outs = []
    for hash_seed in ("1", "2"):
        out = tmp_path / hash_seed
        subprocess.run([sys.executable, "-m", "datagen.generate", "--out", str(out)],
                       cwd=root, check=True, capture_output=True,
                       env={**os.environ, "PYTHONHASHSEED": hash_seed})
        outs.append((out / "nodes" / "flights.csv").read_text())
    assert outs[0] == outs[1]


def test_rotations_are_continuous(dataset):
    gen, _ = dataset
    for ac in gen.aircraft:
        assert ac.legs, ac.tail
        assert ac.legs[0].origin == ac.base
        for prev, nxt in zip(ac.legs, ac.legs[1:]):
            assert nxt.origin == prev.dest
            assert nxt.dep - prev.arr >= MIN_TURN
            assert nxt.leg_seq == prev.leg_seq + 1


def test_every_flight_is_on_a_route(dataset):
    gen, _ = dataset
    for f in gen.flights:
        assert f.dest in gen.routes[f.origin]


def test_hub_dominates(dataset):
    gen, _ = dataset
    touching = sum(1 for f in gen.flights if HUB in (f.origin, f.dest))
    assert touching / len(gen.flights) > 0.6


def test_no_overbooking_and_realistic_loads(dataset):
    gen, _ = dataset
    counts = Counter(b["flight"] for b in gen.bookings)
    for f in gen.flights:
        assert counts[f.flight_id] == f.booked <= f.capacity
        assert f.booked / f.capacity >= 0.70


def test_connections_are_legal(dataset):
    gen, _ = dataset
    flights = {f.flight_id: f for f in gen.flights}
    itineraries = defaultdict(list)
    for b in gen.bookings:
        itineraries[b["pax"]].append((b["segment"], flights[b["flight"]]))
    connecting = [sorted(v, key=lambda t: t[0]) for v in itineraries.values() if len(v) == 2]
    assert len(connecting) > 1000
    for (_, f1), (_, f2) in connecting:
        assert f2.origin == f1.dest
        assert MIN_CONNECT <= f2.dep - f1.arr <= MAX_CONNECT
        assert f2.tail != f1.tail


def test_every_flight_crewed_once_within_duty(dataset):
    gen, _ = dataset
    crewed = Counter()
    for p in gen.pairings:
        for leg in p.flights:
            crewed[leg.flight_id] += 1
        for a, b in zip(p.flights, p.flights[1:]):
            assert b.origin == a.dest and b.dep >= a.arr + 30
        assert p.flights[-1].arr + CREW_DEBRIEF <= p.duty_limit
    assert set(crewed) == {f.flight_id for f in gen.flights}
    assert set(crewed.values()) == {1}


def test_csv_headers_and_referential_integrity(dataset):
    _, out = dataset
    node_ids = set()
    for path in (out / "nodes").glob("*.csv"):
        with path.open() as fh:
            reader = csv.reader(fh)
            header = next(reader)
            assert header[:2] == [":ID", ":LABEL"], path.name
            node_ids.update(row[0] for row in reader)
    edge_ids = set()
    for path in (out / "edges").glob("*.csv"):
        with path.open() as fh:
            reader = csv.reader(fh)
            assert next(reader)[:4] == [":ID", ":START_ID", ":END_ID", ":TYPE"], path.name
            for row in reader:
                assert row[1] in node_ids and row[2] in node_ids, (path.name, row)
                assert row[0] not in edge_ids
                edge_ids.add(row[0])
    assert {f"AP-{c}" for c in AIRPORTS} <= node_ids
