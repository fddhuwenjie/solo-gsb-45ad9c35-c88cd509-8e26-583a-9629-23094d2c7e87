"""End-to-end tests for the POST /fixtures/anonymize API."""

import datetime as dt
import uuid

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ------------------------------------------------------------- fixtures


def users_orders_tables(users_rows=5, orders_rows=10, users_fields=None):
    return [
        {
            "name": "users",
            "rows": users_rows,
            "fields": users_fields
            or {
                "id": {"type": "int", "min_value": 1, "max_value": 100000,
                       "unique": True},
                "name": {"type": "name"},
                "email": {"type": "email", "unique": True},
            },
        },
        {
            "name": "orders",
            "rows": orders_rows,
            "fields": {"total": {"type": "float", "min_value": 0, "max_value": 9999}},
            "foreign_keys": [
                {"column": "user_id", "references_table": "users",
                 "references_column": "id", "nullable": False}
            ],
        },
    ]


def generate(tables, seed=42):
    r = client.post("/fixtures", json={"tables": tables, "seed": seed})
    assert r.status_code == 200, r.text
    return r.json()


def anonymize(fixture, tables, rules, seed=42):
    return client.post(
        "/fixtures/anonymize",
        json={
            "fixture": fixture,
            "tables": tables,
            "sensitive_columns": rules,
            "seed": seed,
        },
    )


# ----------------------------------------------------------------- shape


def test_anonymize_basic_shape_and_seed():
    tables = users_orders_tables()
    fx = generate(tables)
    r = anonymize(
        fx,
        tables,
        [
            {"table": "users", "column": "name"},
            {"table": "users", "column": "email"},
        ],
        seed=123,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["seed"] == 123
    assert data["row_counts"] == {"users": 5, "orders": 10}
    assert set(data["tables"]) == {"users", "orders"}
    assert len(data["tables"]["users"]) == 5
    assert len(data["tables"]["orders"]) == 10


def test_unlisted_columns_are_untouched():
    tables = users_orders_tables()
    fx = generate(tables)
    r = anonymize(fx, tables, [{"table": "users", "column": "name"}], seed=7)
    assert r.status_code == 200, r.text
    out = r.json()["tables"]
    # emails, ids and the whole orders table are copied verbatim
    assert [u["email"] for u in out["users"]] == [u["email"] for u in fx["tables"]["users"]]
    assert [u["id"] for u in out["users"]] == [u["id"] for u in fx["tables"]["users"]]
    assert out["orders"] == fx["tables"]["orders"]


def test_extra_fixture_keys_are_preserved():
    tables = [{"name": "t", "rows": 1,
               "fields": {"n": {"type": "name"}}}]
    fx = generate(tables)
    fx["tables"]["t"][0]["extra"] = {"keep": [1, 2]}
    r = anonymize(fx, tables, [{"table": "t", "column": "n"}], seed=1)
    assert r.status_code == 200, r.text
    row = r.json()["tables"]["t"][0]
    assert row["extra"] == {"keep": [1, 2]}
    assert "n" in row


def test_empty_rule_list_is_verbatim():
    tables = users_orders_tables()
    fx = generate(tables)
    r = anonymize(fx, tables, [], seed=9)
    assert r.status_code == 200
    body = r.json()
    assert body["tables"] == fx["tables"]
    assert body["seed"] == 9


# --------------------------------------------------------- determinism


def test_same_seed_reproducible_different_seed_differs():
    tables = users_orders_tables()
    fx = generate(tables)
    rules = [
        {"table": "users", "column": "name"},
        {"table": "users", "column": "email"},
        {"table": "users", "column": "id"},
        {"table": "orders", "column": "user_id"},
    ]
    r1 = anonymize(fx, tables, rules, seed=55)
    r2 = anonymize(fx, tables, rules, seed=55)
    r3 = anonymize(fx, tables, rules, seed=56)
    assert r1.json() == r2.json()
    assert r1.json()["tables"] != r3.json()["tables"]


# ------------------------------------------------- cross-table consistency


def test_same_value_maps_consistently_across_tables():
    tables = [
        {"name": "a", "rows": 2,
         "fields": {"id": {"type": "int", "min_value": 1, "max_value": 1000,
                            "unique": True},
                    "city": {"type": "string"}}},
        {"name": "b", "rows": 3,
         "fields": {"city2": {"type": "string"}}},
    ]
    fx = {
        "seed": 1, "row_counts": {"a": 2, "b": 3},
        "tables": {
            "a": [{"id": 1, "city": "paris"}, {"id": 2, "city": "lyon"}],
            "b": [{"city2": "paris"}, {"city2": "rome"}, {"city2": "paris"}],
        },
    }
    r = anonymize(
        fx, tables,
        [{"table": "a", "column": "city"}, {"table": "b", "column": "city2"}],
    )
    assert r.status_code == 200, r.text
    out = r.json()["tables"]
    mapping = {row["city"]: row["city"] for row in out["a"]}
    paris = mapping[out["a"][0]["city"]]
    # both "paris" occurrences in b use the same pseudonym as a's
    assert out["b"][0]["city2"] == out["a"][0]["city"]
    assert out["b"][2]["city2"] == out["a"][0]["city"]
    assert paris
    # distinct originals stay distinct
    assert out["a"][0]["city"] != out["a"][1]["city"]


# -------------------------------------------------------------- formats


def test_all_types_stay_parseable_and_shaped():
    tables = [
        {
            "name": "samples",
            "rows": 2,
            "fields": {
                "i": {"type": "int", "min_value": 1000, "max_value": 9999,
                      "unique": True},
                "f": {"type": "float"},
                "b": {"type": "boolean", "unique": True},
                "s": {"type": "string", "prefix": "p_", "min_length": 4,
                      "max_length": 4, "unique": True},
                "n": {"type": "name"},
                "e": {"type": "email", "prefix": "user", "unique": True},
                "u": {"type": "uuid", "unique": True},
                "d": {"type": "date"},
                "dt": {"type": "datetime"},
                "en": {"type": "enum", "choices": ["x", "y", "z"],
                       "unique": True},
            },
        }
    ]
    fx = {
        "seed": 1, "row_counts": {"samples": 2},
        "tables": {
            "samples": [
                {"i": 1234, "f": 12.5, "b": True, "s": "p_abcd",
                 "n": "Alice Adams", "e": "user1@mail.example",
                 "u": "11111111-1111-4111-8111-111111111111",
                 "d": "2021-05-01", "dt": "2021-05-01T10:00:00", "en": "x"},
                {"i": 5678, "f": -3.25, "b": False, "s": "p_efgh",
                 "n": "Bob Brown", "e": "user2@mail.example",
                 "u": "22222222-2222-4222-8222-222222222222",
                 "d": "2022-06-02", "dt": "2022-06-02T11:30:00", "en": "y"},
            ]
        },
    }
    rules = [{"table": "samples", "column": c}
             for c in ("i", "f", "b", "s", "n", "e", "u", "d", "dt", "en")]
    r = anonymize(fx, tables, rules, seed=2)
    assert r.status_code == 200, r.text
    rows = r.json()["tables"]["samples"]
    for row in rows:
        assert isinstance(row["i"], int) and 1000 <= row["i"] <= 9999
        assert len(str(row["i"])) == 4  # digit width preserved
        assert isinstance(row["f"], float)
        assert isinstance(row["b"], bool)
        assert row["s"].startswith("p_") and len(row["s"]) == 6
        assert len(row["n"].split()) == 2
        assert row["e"].startswith("user") and "@" in row["e"]
        uuid.UUID(row["u"])
        dt.date.fromisoformat(row["d"])
        dt.datetime.fromisoformat(row["dt"])
        assert row["en"] in {"x", "y", "z"}
    assert len({row["i"] for row in rows}) == 2
    assert len({row["u"] for row in rows}) == 2
    assert len({row["en"] for row in rows}) == 2
    assert {row["b"] for row in rows} == {True, False}


def test_email_domain_is_preserved():
    tables = [{"name": "t", "rows": 2,
               "fields": {"e": {"type": "email", "unique": True}}}]
    fx = {"seed": 1, "row_counts": {"t": 2}, "tables": {"t": [
        {"e": "alice@corp-a.example"}, {"e": "bob@corp-b.example"}]}}
    r = anonymize(fx, tables, [{"table": "t", "column": "e"}], seed=1)
    assert r.status_code == 200, r.text
    out = [x["e"] for x in r.json()["tables"]["t"]]
    assert out[0].endswith("@corp-a.example")
    assert out[1].endswith("@corp-b.example")


def test_null_values_are_always_preserved():
    tables = [{"name": "t", "rows": 3,
               "fields": {"x": {"type": "int", "nullable": True},
                          "y": {"type": "name", "nullable": True}}}]
    fx = {"seed": 1, "row_counts": {"t": 3}, "tables": {"t": [
        {"x": None, "y": None},
        {"x": 5, "y": None},
        {"x": 6, "y": "A B"},
    ]}}
    r = anonymize(fx, tables,
                  [{"table": "t", "column": "x"}, {"table": "t", "column": "y"}])
    assert r.status_code == 200, r.text
    rows = r.json()["tables"]["t"]
    assert rows[0] == {"x": None, "y": None}
    assert isinstance(rows[1]["x"], int) and rows[1]["y"] is None
    assert rows[2]["y"] is not None and len(rows[2]["y"].split()) == 2


# ------------------------------------------------------------ uniqueness


def test_single_column_unique_has_no_collisions():
    tables = users_orders_tables(
        users_rows=200, orders_rows=1,
        users_fields={
            "id": {"type": "int", "min_value": 1, "max_value": 100000,
                   "unique": True},
            "name": {"type": "name"},
            "email": {"type": "email", "unique": True},
        },
    )
    fx = generate(tables)
    r = anonymize(
        fx, tables,
        [{"table": "users", "column": "id"},
         {"table": "users", "column": "email"},
         {"table": "orders", "column": "user_id"}],
        seed=11,
    )
    assert r.status_code == 200, r.text
    rows = r.json()["tables"]["users"]
    assert len({u["id"] for u in rows}) == 200
    assert len({u["email"] for u in rows}) == 200


def test_composite_unique_constraint_is_respected():
    tables = [
        {
            "name": "slots",
            "rows": 12,
            "fields": {
                "day": {"type": "enum", "choices": ["mon", "tue", "wed"],
                        "nullable": False},
                "hour": {"type": "enum", "choices": ["10", "11", "12", "13"],
                         "nullable": False},
            },
            "unique_constraints": [["day", "hour"]],
        }
    ]
    fx = generate(tables, seed=5)
    r = anonymize(
        fx, tables,
        [{"table": "slots", "column": "day"},
         {"table": "slots", "column": "hour"}],
        seed=8,
    )
    assert r.status_code == 200, r.text
    rows = r.json()["tables"]["slots"]
    pairs = {(row["day"], row["hour"]) for row in rows}
    assert len(pairs) == 12
    for day, hour in pairs:
        assert day in {"mon", "tue", "wed"}
        assert hour in {"10", "11", "12", "13"}


def test_composite_unique_null_semantics():
    # SQL semantics: tuples containing NULL never collide.
    tables = [
        {
            "name": "bookings",
            "rows": 3,
            "fields": {
                "room": {"type": "int", "min_value": 1, "max_value": 9},
                "guest": {"type": "string"},
            },
            "unique_constraints": [["room", "guest"]],
        }
    ]
    fx = {"seed": 1, "row_counts": {"bookings": 3}, "tables": {"bookings": [
        {"room": 1, "guest": None},
        {"room": 2, "guest": None},
        {"room": 1, "guest": "bob"},
    ]}}
    r = anonymize(
        fx, tables,
        [{"table": "bookings", "column": "room"},
         {"table": "bookings", "column": "guest"}],
    )
    assert r.status_code == 200, r.text
    rows = r.json()["tables"]["bookings"]
    assert rows[0]["guest"] is None and rows[1]["guest"] is None
    assert rows[0]["room"] != rows[1]["room"]


def test_enum_unique_remains_a_permutation():
    tables = [{"name": "t", "rows": 3, "fields": {
        "c": {"type": "enum", "choices": ["x", "y", "z"], "unique": True}}}]
    fx = {"seed": 1, "row_counts": {"t": 3}, "tables": {"t": [
        {"c": "x"}, {"c": "y"}, {"c": "z"}]}}
    r = anonymize(fx, tables, [{"table": "t", "column": "c"}], seed=1)
    assert r.status_code == 200
    assert {row["c"] for row in r.json()["tables"]["t"]} == {"x", "y", "z"}


def test_uniqueness_impossible_is_422():
    # Only 20*30 = 600 distinct names exist.
    tables = [{"name": "t", "rows": 601,
               "fields": {"n": {"type": "name", "unique": True}}}]
    fx = {"seed": 1, "row_counts": {"t": 601},
          "tables": {"t": [{"n": f"First{i} Last{i}"} for i in range(601)]}}
    r = anonymize(fx, tables, [{"table": "t", "column": "n"}], seed=1)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unique_space_exhausted"
    assert set(r.json()) == {"detail"}


# ----------------------------------------------------------- foreign keys


def test_anonymized_fk_still_points_at_anonymized_target():
    tables = users_orders_tables()
    fx = generate(tables)
    rules = [
        {"table": "users", "column": "id"},
        {"table": "orders", "column": "user_id"},
    ]
    r = anonymize(fx, tables, rules, seed=3)
    assert r.status_code == 200, r.text
    out = r.json()["tables"]
    parent_ids = {u["id"] for u in out["users"]}
    assert len(parent_ids) == 5
    for order in out["orders"]:
        assert order["user_id"] in parent_ids


def test_fk_listed_without_target_keeps_raw_target_values():
    tables = users_orders_tables()
    fx = generate(tables)
    r = anonymize(fx, tables, [{"table": "orders", "column": "user_id"}])
    assert r.status_code == 200, r.text
    out = r.json()["tables"]
    raw_ids = {u["id"] for u in fx["tables"]["users"]}
    # Nothing was rewritten: parent ids and FK values stay raw.
    assert [u["id"] for u in out["users"]] == [u["id"] for u in fx["tables"]["users"]]
    assert all(o["user_id"] in raw_ids for o in out["orders"])


def test_anonymized_target_requires_fk_in_rules_otherwise_422():
    tables = users_orders_tables()
    fx = generate(tables)
    r = anonymize(fx, tables, [{"table": "users", "column": "id"}])
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "fk_target_anonymized"
    assert set(r.json()) == {"detail"}


def test_self_reference_trees_stay_intact():
    tables = [
        {
            "name": "nodes",
            "rows": 50,
            "fields": {"id": {"type": "int", "min_value": 1, "max_value": 1000,
                              "unique": True}},
            "foreign_keys": [
                {"column": "parent_id", "references_table": "nodes",
                 "references_column": "id", "nullable": True}
            ],
        }
    ]
    fx = generate(tables, seed=4)
    r = anonymize(
        fx, tables,
        [{"table": "nodes", "column": "id"},
         {"table": "nodes", "column": "parent_id"}],
        seed=9,
    )
    assert r.status_code == 200, r.text
    rows = r.json()["tables"]["nodes"]
    ids = {row["id"] for row in rows}
    assert len(ids) == 50
    assert rows[0]["parent_id"] is None
    earlier: set = set()
    for row in rows:
        if row["parent_id"] is not None:
            assert row["parent_id"] in ids
            assert row["parent_id"] in earlier
        earlier.add(row["id"])
    # null positions are preserved
    raw_rows = fx["tables"]["nodes"]
    assert [row["parent_id"] is None for row in rows] == [
        row["parent_id"] is None for row in raw_rows
    ]


def test_diamond_foreign_keys_roundtrip():
    tables = [
        {"name": "companies", "rows": 10, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 100000,
                   "unique": True},
            "email": {"type": "email", "unique": True}}},
        {"name": "departments", "rows": 20, "fields": {
            "dept_no": {"type": "int", "min_value": 1, "max_value": 100000,
                        "unique": True},
            "title": {"type": "name"}},
         "foreign_keys": [
             {"column": "company_id", "references_table": "companies",
              "nullable": False}]},
        {"name": "employees", "rows": 40, "fields": {
            "full_name": {"type": "name"}},
         "foreign_keys": [
             {"column": "company_id", "references_table": "companies",
              "nullable": False},
             {"column": "dept_no", "references_table": "departments",
              "references_column": "dept_no", "nullable": True}]},
    ]
    fx = generate(tables, seed=21)
    rules = [
        {"table": "companies", "column": "id"},
        {"table": "companies", "column": "email"},
        {"table": "departments", "column": "dept_no"},
        {"table": "departments", "column": "title"},
        {"table": "departments", "column": "company_id"},
        {"table": "employees", "column": "full_name"},
        {"table": "employees", "column": "company_id"},
        {"table": "employees", "column": "dept_no"},
    ]
    r = anonymize(fx, tables, rules, seed=42)
    assert r.status_code == 200, r.text
    out = r.json()["tables"]
    company_ids = {c["id"] for c in out["companies"]}
    dept_nos = {d["dept_no"] for d in out["departments"]}
    assert all(d["company_id"] in company_ids for d in out["departments"])
    for e in out["employees"]:
        assert e["company_id"] in company_ids
        assert e["dept_no"] is None or e["dept_no"] in dept_nos


# ------------------------------------------------------------- 422 paths


def test_rule_unknown_table_is_422():
    tables = users_orders_tables()
    fx = generate(tables)
    r = anonymize(fx, tables, [{"table": "ghosts", "column": "id"}])
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unknown_table"


def test_rule_unknown_column_is_422():
    tables = users_orders_tables()
    fx = generate(tables)
    r = anonymize(fx, tables, [{"table": "users", "column": "nope"}])
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unknown_column"


def test_duplicate_rule_is_422():
    tables = [{"name": "t", "rows": 1, "fields": {"n": {"type": "name"}}}]
    fx = generate(tables)
    rules = [{"table": "t", "column": "n"}, {"table": "t", "column": "n"}]
    r = anonymize(fx, tables, rules)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "duplicate_rule"


def test_unsupported_value_format_is_422():
    tables = [{"name": "t", "rows": 1, "fields": {"e": {"type": "email"}}}]
    fx = {"seed": 1, "row_counts": {"t": 1},
          "tables": {"t": [{"e": "not-an-email"}]}}
    r = anonymize(fx, tables, [{"table": "t", "column": "e"}])
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unsupported_type"


def test_malformed_uuid_is_422():
    tables = [{"name": "t", "rows": 1, "fields": {"u": {"type": "uuid"}}}]
    fx = {"seed": 1, "row_counts": {"t": 1},
          "tables": {"t": [{"u": "abc"}]}}
    r = anonymize(fx, tables, [{"table": "t", "column": "u"}])
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unsupported_type"


def test_fixture_row_count_mismatch_is_422():
    tables = users_orders_tables(users_rows=5)
    fx = generate(tables)
    tables[0]["rows"] = 6
    r = anonymize(fx, tables, [{"table": "users", "column": "name"}])
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "fixture_shape_mismatch"


def test_fixture_reported_row_counts_mismatch_is_422():
    tables = users_orders_tables(users_rows=5)
    fx = generate(tables)
    fx["row_counts"]["users"] = 999
    r = anonymize(fx, tables, [])
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "fixture_shape_mismatch"


def test_synthesized_fk_column_can_be_anonymized():
    # user_id is not declared in orders.fields; the generator synthesises it
    # from users.id, and anonymization must follow that mapping too.
    tables = users_orders_tables(users_rows=5)
    fx = generate(tables, seed=4)
    rules = [
        {"table": "users", "column": "id"},
        {"table": "users", "column": "name"},
        {"table": "orders", "column": "user_id"},
    ]
    r = anonymize(fx, tables, rules, seed=17)
    assert r.status_code == 200, r.text
    out = r.json()["tables"]
    parent_ids = {u["id"] for u in out["users"]}
    assert all(o["user_id"] in parent_ids for o in out["orders"])
    assert set(out["orders"][0]) == {"total", "user_id"}
    # non-listed columns untouched
    assert [o["total"] for o in out["orders"]] == [
        o["total"] for o in fx["tables"]["orders"]
    ]


def test_fixture_missing_column_is_422():
    tables = users_orders_tables()
    fx = generate(tables)
    del fx["tables"]["users"][0]["email"]
    r = anonymize(fx, tables, [{"table": "users", "column": "name"}])
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "fixture_shape_mismatch"


def test_fixture_table_set_mismatch_is_422():
    tables = users_orders_tables()
    fx = generate(tables)
    fx["tables"].pop("orders")
    r = anonymize(fx, tables, [{"table": "users", "column": "name"}])
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "fixture_shape_mismatch"


def test_raw_fixture_fk_violation_is_422():
    tables = users_orders_tables()
    fx = generate(tables)
    fx["tables"]["orders"][0]["user_id"] = 999999
    r = anonymize(fx, tables, [])
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "fixture_fk_violation"


def test_raw_fixture_unique_violation_is_422():
    tables = [{"name": "t", "rows": 2,
               "fields": {"x": {"type": "int", "unique": True}}}]
    fx = {"seed": 1, "row_counts": {"t": 2},
          "tables": {"t": [{"x": 5}, {"x": 5}]}}
    r = anonymize(fx, tables, [{"table": "t", "column": "x"}])
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "fixture_unique_violation"


def test_inconsistent_table_schema_rejected_like_generator():
    # Cycle in FK graph -> planner validation reused, 422, no partial data.
    tables = [
        {"name": "a", "rows": 1, "fields": {"id": {"type": "int", "unique": True}},
         "foreign_keys": [{"column": "b_id", "references_table": "b",
                           "references_column": "id"}]},
        {"name": "b", "rows": 1, "fields": {"id": {"type": "int", "unique": True}},
         "foreign_keys": [{"column": "a_id", "references_table": "a",
                           "references_column": "id"}]},
    ]
    fx = {"seed": 1, "row_counts": {"a": 1, "b": 1},
          "tables": {"a": [], "b": []}}
    r = anonymize(fx, tables, [])
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "dependency_cycle"
    assert set(r.json()) == {"detail"}


def test_missing_required_body_fields_is_422():
    r = client.post("/fixtures/anonymize", json={})
    assert r.status_code == 422
    r = client.post("/fixtures/anonymize", json={"tables": [], "fixture": {}})
    assert r.status_code == 422


def test_zero_row_tables_roundtrip():
    tables = [
        {"name": "u", "rows": 0,
         "fields": {"id": {"type": "int", "min_value": 1, "max_value": 9,
                            "unique": True}}},
        {"name": "o", "rows": 0, "fields": {"total": {"type": "float"}},
         "foreign_keys": [{"column": "uid", "references_table": "u",
                           "nullable": True}]},
    ]
    fx = generate(tables)
    r = anonymize(
        fx, tables,
        [{"table": "u", "column": "id"}, {"table": "o", "column": "uid"}],
    )
    assert r.status_code == 200, r.text
    assert r.json()["tables"] == {"u": [], "o": []}
    assert r.json()["row_counts"] == {"u": 0, "o": 0}


def test_extra_request_fields_forbidden():
    tables = [{"name": "t", "rows": 1, "fields": {"n": {"type": "name"}}}]
    fx = generate(tables)
    r = client.post("/fixtures/anonymize", json={
        "fixture": fx, "tables": tables, "sensitive_columns": [],
        "locale": "zh",
    })
    assert r.status_code == 422
