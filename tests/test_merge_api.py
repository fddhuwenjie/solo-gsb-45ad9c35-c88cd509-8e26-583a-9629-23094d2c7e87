"""End-to-end tests for the POST /fixtures/merge API."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ------------------------------------------------------------- fixtures


def users_orders_schema():
    return [
        {"name": "users", "rows": 2, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 100000,
                   "unique": True},
            "email": {"type": "email", "unique": True},
            "name": {"type": "name"}}},
        {"name": "orders", "rows": 2, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 100000,
                   "unique": True},
            "total": {"type": "float"}},
         "foreign_keys": [
             {"column": "user_id", "references_table": "users",
              "nullable": False}]},
    ]


def fx(users, orders, seed=1):
    return {
        "seed": seed,
        "row_counts": {"users": len(users), "orders": len(orders)},
        "tables": {"users": users, "orders": orders},
    }


def merge(fixtures, tables, seed=42):
    return client.post("/fixtures/merge", json={
        "fixtures": fixtures, "tables": tables, "seed": seed})


def generate(tables, seed=42):
    r = client.post("/fixtures", json={"tables": tables, "seed": seed})
    assert r.status_code == 200, r.text
    return r.json()


# ----------------------------------------------------------------- basics


def test_merge_identical_rows_are_deduplicated():
    tables = users_orders_schema()
    fx1 = fx(
        [{"id": 1, "email": "a@x.example", "name": "A B"},
         {"id": 2, "email": "b@x.example", "name": "C D"}],
        [{"id": 10, "total": 1.5, "user_id": 1}],
    )
    fx2 = fx(
        [{"id": 1, "email": "a@x.example", "name": "A B"},   # identical copy
         {"id": 3, "email": "c@x.example", "name": "E F"}],
        [{"id": 10, "total": 1.5, "user_id": 1}],            # identical copy
        seed=2,
    )
    r = merge([fx1, fx2], tables)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["row_counts"] == {"users": 3, "orders": 1}
    assert data["tables"]["users"] == (
        fx1["tables"]["users"] + [fx2["tables"]["users"][1]]
    )
    assert data["tables"]["orders"] == fx1["tables"]["orders"]


def test_merge_merging_fixture_with_itself_is_a_noop():
    tables = users_orders_schema()
    fx1 = fx(
        [{"id": 1, "email": "a@x.example", "name": "A B"},
         {"id": 2, "email": "b@x.example", "name": "C D"}],
        [{"id": 10, "total": 1.5, "user_id": 1},
         {"id": 11, "total": 2.5, "user_id": 2}],
    )
    r = merge([fx1, fx1], tables)
    assert r.status_code == 200, r.text
    assert r.json()["tables"] == fx1["tables"]
    assert r.json()["row_counts"] == {"users": 2, "orders": 2}


def test_merge_disjoint_fixtures_are_concatenated_verbatim():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}],
             [{"id": 10, "total": 1.5, "user_id": 1}])
    fx2 = fx([{"id": 2, "email": "b@x.example", "name": "C D"}],
             [{"id": 11, "total": 2.5, "user_id": 2}], seed=2)
    r = merge([fx1, fx2], tables)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["tables"]["users"] == (
        fx1["tables"]["users"] + fx2["tables"]["users"]
    )
    assert data["tables"]["orders"] == (
        fx1["tables"]["orders"] + fx2["tables"]["orders"]
    )


def test_merge_conflicting_unique_key_rewrites_and_cascades():
    tables = users_orders_schema()
    fx1 = fx(
        [{"id": 1, "email": "a@x.example", "name": "A B"},
         {"id": 2, "email": "b@x.example", "name": "C D"}],
        [{"id": 10, "total": 1.5, "user_id": 1},
         {"id": 11, "total": 2.5, "user_id": 2}],
    )
    fx2 = fx(
        [{"id": 1, "email": "a@x.example", "name": "A B"},   # deduplicated
         {"id": 2, "email": "c@x.example", "name": "E F"}],  # id clash
        [{"id": 10, "total": 9.5, "user_id": 1},             # id clash
         {"id": 12, "total": 3.5, "user_id": 2}],            # follows its user
        seed=2,
    )
    r = merge([fx1, fx2], tables, seed=7)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["seed"] == 7
    assert data["row_counts"] == {"users": 3, "orders": 4}

    users = data["tables"]["users"]
    # row order is stable: fixture order, then original row order
    assert [u["email"] for u in users] == [
        "a@x.example", "b@x.example", "c@x.example"]
    new_id = users[2]["id"]
    assert isinstance(new_id, int) and 1 <= new_id <= 100000
    assert new_id not in (1, 2)
    # non-conflicting values of the rewritten row are preserved verbatim
    assert users[2]["name"] == "E F"

    orders = data["tables"]["orders"]
    assert [o["total"] for o in orders] == [1.5, 2.5, 9.5, 3.5]
    assert orders[2]["id"] not in (10, 11, 12)
    assert orders[2]["user_id"] == 1        # still points at the deduplicated user
    assert orders[3]["id"] == 12            # no clash: untouched
    assert orders[3]["user_id"] == new_id   # cascade followed the rewrite
    user_ids = {u["id"] for u in users}
    assert all(o["user_id"] in user_ids for o in orders)


def test_merge_extra_fields_and_untouched_rows_are_verbatim():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B",
               "extra": {"keep": [1, 2]}}],
             [{"id": 10, "total": 1.5, "user_id": 1, "note": "x"}])
    fx2 = fx([{"id": 1, "email": "b@x.example", "name": "C D",
               "extra": "also-kept"}],
             [{"id": 11, "total": 2.5, "user_id": 1}], seed=2)
    r = merge([fx1, fx2], tables)
    assert r.status_code == 200, r.text
    out = r.json()["tables"]
    assert out["users"][0] == fx1["tables"]["users"][0]      # untouched row
    assert out["users"][1]["extra"] == "also-kept"           # extra field kept
    assert out["users"][1]["id"] != 1                        # but id rewritten
    # fx1's order is verbatim; fx2's order followed its own user's rewrite
    assert out["orders"][0] == fx1["tables"]["orders"][0]
    assert out["orders"][1]["user_id"] == out["users"][1]["id"]
    assert out["orders"][1]["total"] == 2.5


def test_merge_table_order_follows_schema_declaration():
    tables = list(reversed(users_orders_schema()))  # child declared first
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}],
             [{"id": 10, "total": 1.5, "user_id": 1}])
    fx2 = fx([{"id": 2, "email": "b@x.example", "name": "C D"}],
             [{"id": 11, "total": 2.5, "user_id": 2}], seed=2)
    r = merge([fx1, fx2], tables)
    assert r.status_code == 200, r.text
    assert list(r.json()["tables"]) == ["orders", "users"]


def test_merge_null_unique_values_never_collide():
    tables = [{"name": "t", "rows": 2, "fields": {
        "x": {"type": "int", "unique": True, "nullable": True}}}]
    fx1 = {"seed": 1, "row_counts": {"t": 2}, "tables": {"t": [
        {"x": None, "tag": "a"}, {"x": 1, "tag": "b"}]}}
    fx2 = {"seed": 2, "row_counts": {"t": 2}, "tables": {"t": [
        {"x": None, "tag": "a"}, {"x": 1, "tag": "c"}]}}
    r = merge([fx1, fx2], tables)
    assert r.status_code == 200, r.text
    rows = r.json()["tables"]["t"]
    # identical rows whose only unique key is NULL are both kept (SQL semantics)
    assert rows[0] == {"x": None, "tag": "a"}
    assert rows[2] == {"x": None, "tag": "a"}
    # the non-null clash is rewritten
    assert rows[1]["x"] == 1
    assert rows[3]["x"] is not None and rows[3]["x"] != 1
    assert rows[3]["tag"] == "c"


def test_merge_constraint_free_table_keeps_duplicate_rows():
    tables = [{"name": "logs", "rows": 1,
               "fields": {"msg": {"type": "string"}}}]
    fx1 = {"seed": 1, "row_counts": {"logs": 1},
           "tables": {"logs": [{"msg": "same"}]}}
    fx2 = {"seed": 2, "row_counts": {"logs": 1},
           "tables": {"logs": [{"msg": "same"}]}}
    r = merge([fx1, fx2], tables)
    assert r.status_code == 200, r.text
    # no unique key -> nothing can conflict -> both rows stay
    assert r.json()["tables"]["logs"] == [{"msg": "same"}, {"msg": "same"}]


# ------------------------------------------------------- composite unique


def test_merge_composite_unique_conflict_is_rewritten():
    tables = [{"name": "slots", "rows": 2, "fields": {
        "day": {"type": "enum", "choices": ["mon", "tue"]},
        "hour": {"type": "enum", "choices": ["10", "11"]},
        "note": {"type": "string"}},
        "unique_constraints": [["day", "hour"]]}]
    fx1 = {"seed": 1, "row_counts": {"slots": 2}, "tables": {"slots": [
        {"day": "mon", "hour": "10", "note": "aaaaaaaa"},
        {"day": "mon", "hour": "11", "note": "bbbbbbbb"},
    ]}}
    fx2 = {"seed": 2, "row_counts": {"slots": 2}, "tables": {"slots": [
        {"day": "mon", "hour": "10", "note": "cccccccc"},  # tuple clash
        {"day": "tue", "hour": "10", "note": "dddddddd"},
    ]}}
    r = merge([fx1, fx2], tables)
    assert r.status_code == 200, r.text
    rows = r.json()["tables"]["slots"]
    assert len(rows) == 4
    keys = [(x["day"], x["hour"]) for x in rows]
    assert len(set(keys)) == 4
    # the first occurrence is kept verbatim; the clash moved to the only
    # free combination (tue, 11) and kept its other fields
    assert rows[0] == {"day": "mon", "hour": "10", "note": "aaaaaaaa"}
    assert rows[2]["note"] == "cccccccc"
    assert (rows[2]["day"], rows[2]["hour"]) == ("tue", "11")
    assert rows[3] == {"day": "tue", "hour": "10", "note": "dddddddd"}


def test_merge_composite_unique_space_exhausted_is_422():
    tables = [{"name": "slots", "rows": 3, "fields": {
        "day": {"type": "enum", "choices": ["mon", "tue"]},
        "hour": {"type": "enum", "choices": ["10", "11"]},
        "note": {"type": "string"}},
        "unique_constraints": [["day", "hour"]]}]
    fx1 = {"seed": 1, "row_counts": {"slots": 3}, "tables": {"slots": [
        {"day": "mon", "hour": "10", "note": "aaaaaaaa"},
        {"day": "mon", "hour": "11", "note": "bbbbbbbb"},
        {"day": "tue", "hour": "10", "note": "cccccccc"},
    ]}}
    fx2 = {"seed": 2, "row_counts": {"slots": 3}, "tables": {"slots": [
        {"day": "mon", "hour": "10", "note": "dddddddd"},
        {"day": "mon", "hour": "11", "note": "eeeeeeee"},
        {"day": "tue", "hour": "11", "note": "ffffffff"},
    ]}}
    r = merge([fx1, fx2], tables)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unique_space_exhausted"
    assert set(r.json()) == {"detail"}


# ----------------------------------------------------------- foreign keys


def test_merge_unique_fk_column_repicks_from_parent_pool():
    tables = [
        {"name": "users", "rows": 2, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 100,
                   "unique": True}}},
        {"name": "orders", "rows": 1, "fields": {
            "user_id": {"type": "int", "unique": True},
            "total": {"type": "float"}},
         "foreign_keys": [
             {"column": "user_id", "references_table": "users",
              "nullable": False}]},
    ]
    fx1 = {"seed": 1, "row_counts": {"users": 2, "orders": 1}, "tables": {
        "users": [{"id": 1}, {"id": 2}],
        "orders": [{"user_id": 1, "total": 1.5}],
    }}
    fx2 = {"seed": 2, "row_counts": {"users": 2, "orders": 1}, "tables": {
        "users": [{"id": 1}, {"id": 2}],  # identical: deduplicated
        "orders": [{"user_id": 1, "total": 2.5}],  # unique FK clash
    }}
    r = merge([fx1, fx2], tables)
    assert r.status_code == 200, r.text
    orders = r.json()["tables"]["orders"]
    assert [o["total"] for o in orders] == [1.5, 2.5]
    # the clashing FK value is repicked from the existing parent values
    assert sorted(o["user_id"] for o in orders) == [1, 2]


def test_merge_unique_fk_pool_exhausted_is_422():
    tables = [
        {"name": "users", "rows": 1, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 100,
                   "unique": True}}},
        {"name": "orders", "rows": 1, "fields": {
            "user_id": {"type": "int", "unique": True},
            "total": {"type": "float"}},
         "foreign_keys": [
             {"column": "user_id", "references_table": "users",
              "nullable": False}]},
    ]
    fx1 = {"seed": 1, "row_counts": {"users": 1, "orders": 1}, "tables": {
        "users": [{"id": 1}], "orders": [{"user_id": 1, "total": 1.5}]}}
    fx2 = {"seed": 2, "row_counts": {"users": 1, "orders": 1}, "tables": {
        "users": [{"id": 1}], "orders": [{"user_id": 1, "total": 2.5}]}}
    r = merge([fx1, fx2], tables)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unique_space_exhausted"
    assert set(r.json()) == {"detail"}


def test_merge_cascade_flows_through_multi_level_chain():
    tables = [
        {"name": "companies", "rows": 1, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 1000,
                   "unique": True},
            "name": {"type": "string"}}},
        {"name": "departments", "rows": 1, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 1000,
                   "unique": True}},
         "foreign_keys": [
             {"column": "company_id", "references_table": "companies",
              "nullable": False}]},
        {"name": "employees", "rows": 1, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 1000,
                   "unique": True}},
         "foreign_keys": [
             {"column": "dept_id", "references_table": "departments",
              "nullable": False}]},
    ]

    def chain_fx(company_name, seed):
        return {"seed": seed,
                "row_counts": {"companies": 1, "departments": 1,
                               "employees": 1},
                "tables": {
                    "companies": [{"id": 1, "name": company_name}],
                    "departments": [{"id": 10, "company_id": 1}],
                    "employees": [{"id": 100, "dept_id": 10}],
                }}

    r = merge([chain_fx("aaaaaaaa", 1), chain_fx("bbbbbbbb", 2)], tables)
    assert r.status_code == 200, r.text
    out = r.json()["tables"]
    # every level clashed and was rewritten; references followed along
    assert out["companies"][0] == {"id": 1, "name": "aaaaaaaa"}
    new_company = out["companies"][1]
    assert new_company["id"] != 1 and new_company["name"] == "bbbbbbbb"
    new_dept = out["departments"][1]
    assert new_dept["id"] != 10
    assert new_dept["company_id"] == new_company["id"]
    new_emp = out["employees"][1]
    assert new_emp["id"] != 100
    assert new_emp["dept_id"] == new_dept["id"]
    # first fixture's rows are untouched
    assert out["departments"][0] == {"id": 10, "company_id": 1}
    assert out["employees"][0] == {"id": 100, "dept_id": 10}


def test_merge_self_referencing_tree_cascades():
    tables = [{"name": "nodes", "rows": 2, "fields": {
        "id": {"type": "int", "min_value": 1, "max_value": 1000,
               "unique": True}},
        "foreign_keys": [
            {"column": "parent_id", "references_table": "nodes",
             "references_column": "id", "nullable": True}]}]
    fx1 = {"seed": 1, "row_counts": {"nodes": 1}, "tables": {"nodes": [
        {"id": 1, "parent_id": None},
    ]}}
    fx2 = {"seed": 2, "row_counts": {"nodes": 2}, "tables": {"nodes": [
        {"id": 1, "parent_id": None, "tag": "x"},   # id clash -> rewritten
        {"id": 5, "parent_id": 1},                  # follows the rewrite
    ]}}
    r = merge([fx1, fx2], tables)
    assert r.status_code == 200, r.text
    rows = r.json()["tables"]["nodes"]
    assert len(rows) == 3
    assert rows[0] == {"id": 1, "parent_id": None}
    new_root = rows[1]
    assert new_root["id"] != 1 and new_root["parent_id"] is None
    assert new_root["tag"] == "x"                  # extra field preserved
    assert rows[2]["id"] == 5
    assert rows[2]["parent_id"] == new_root["id"]  # self-reference cascaded


# ------------------------------------------- multi-level cascade relinking


def abc_schema():
    """A -> B -> C where C references B's unique FK column (B.a_id)."""
    return [
        {"name": "a", "rows": 1, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 1000,
                   "unique": True},
            "name": {"type": "string"}}},
        {"name": "b", "rows": 1, "fields": {
            "a_id": {"type": "int", "unique": True},
            "label": {"type": "string"}},
         "foreign_keys": [
             {"column": "a_id", "references_table": "a",
              "nullable": False}]},
        {"name": "c", "rows": 1, "fields": {
            "b_ref": {"type": "int"},
            "note": {"type": "string"}},
         "foreign_keys": [
             {"column": "b_ref", "references_table": "b",
              "references_column": "a_id", "nullable": False}]},
    ]


def abc_fx(a_name, b_label, c_note, seed):
    return {"seed": seed,
            "row_counts": {"a": 1, "b": 1, "c": 1},
            "tables": {
                "a": [{"id": 1, "name": a_name}],
                "b": [{"a_id": 1, "label": b_label}],
                "c": [{"b_ref": 1, "note": c_note}],
            }}


def test_merge_three_level_chain_relinks_grandchildren():
    tables = abc_schema()
    fx1 = abc_fx("aaaaaaaa", "bbbbbbbb", "cccccccc", 1)
    fx2 = abc_fx("dddddddd", "eeeeeeee", "ffffffff", 2)
    r = merge([fx1, fx2], tables, seed=11)
    assert r.status_code == 200, r.text
    out = r.json()["tables"]
    # the first fixture's whole chain is untouched
    assert out["a"][0] == {"id": 1, "name": "aaaaaaaa"}
    assert out["b"][0] == {"a_id": 1, "label": "bbbbbbbb"}
    assert out["c"][0] == {"b_ref": 1, "note": "cccccccc"}
    # fixture 2's clashing a.id was rewritten; the rewrite propagated
    # through b into c, so the grandchild points at its own fixture's
    # b row instead of being stranded on fixture 1's key
    new_key = out["a"][1]["id"]
    assert new_key != 1 and out["a"][1]["name"] == "dddddddd"
    assert out["b"][1]["a_id"] == new_key
    assert out["b"][1]["label"] == "eeeeeeee"
    assert out["c"][1]["b_ref"] == new_key
    assert out["c"][1]["note"] == "ffffffff"


def test_merge_consecutive_cascades_compose_across_four_levels():
    # Every level's referenced column is itself the FK to the level above,
    # so the top-level rewrite must be re-published at each hop instead of
    # letting lower levels re-roll their key from the parent pool (with
    # three fixtures the pool holds several free keys and a re-roll can
    # cross the chains between fixtures).
    tables = [
        {"name": "a", "rows": 1, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 1000,
                   "unique": True}}},
        {"name": "b", "rows": 1, "fields": {
            "a_id": {"type": "int", "unique": True}},
         "foreign_keys": [
             {"column": "a_id", "references_table": "a",
              "nullable": False}]},
        {"name": "c", "rows": 1, "fields": {
            "b_key": {"type": "int", "unique": True}},
         "foreign_keys": [
             {"column": "b_key", "references_table": "b",
              "references_column": "a_id", "nullable": False}]},
        {"name": "d", "rows": 1, "fields": {
            "c_key": {"type": "int"}},
         "foreign_keys": [
             {"column": "c_key", "references_table": "c",
              "references_column": "b_key", "nullable": False}]},
    ]

    def chain_fx(tag, seed):
        # same clashing key on every level; the extra tag field keeps the
        # rows distinct so nothing is deduplicated
        return {"seed": seed,
                "row_counts": {"a": 1, "b": 1, "c": 1, "d": 1},
                "tables": {
                    "a": [{"id": 1, "tag": tag}],
                    "b": [{"a_id": 1, "tag": tag}],
                    "c": [{"b_key": 1, "tag": tag}],
                    "d": [{"c_key": 1, "tag": tag}],
                }}

    r = merge([chain_fx("one", 1), chain_fx("two", 2), chain_fx("three", 3)],
              tables, seed=1)
    assert r.status_code == 200, r.text
    out = r.json()["tables"]
    levels = (("a", "id"), ("b", "a_id"), ("c", "b_key"), ("d", "c_key"))
    # fixture 1 keeps the original key on every level
    assert [out[t][0][col] for t, col in levels] == [1] * 4
    # every other fixture's key was rewritten once at the top and the
    # rewrite composed through every consecutive hop: within each fixture
    # all four levels agree on that fixture's final key, the keys of
    # different fixtures stay distinct, and extra fields are verbatim
    used = {1}
    for i, tag in ((1, "two"), (2, "three")):
        chain = [out[t][i][col] for t, col in levels]
        assert len(set(chain)) == 1
        assert chain[0] not in used
        used.add(chain[0])
        assert [out[t][i]["tag"] for t in ("a", "b", "c", "d")] == [tag] * 4


def test_merge_three_level_child_follows_parents_own_rewrite():
    tables = abc_schema()
    fx1 = abc_fx("aaaaaaaa", "bbbbbbbb", "cccccccc", 1)
    fx2 = {"seed": 2, "row_counts": {"a": 2, "b": 1, "c": 1}, "tables": {
        "a": [{"id": 1, "name": "aaaaaaaa"},   # identical to fx1: deduplicated
              {"id": 2, "name": "dddddddd"}],
        "b": [{"a_id": 1, "label": "eeeeeeee"}],  # a_id clash, no cascade
        "c": [{"b_ref": 1, "note": "ffffffff"}],
    }}
    r = merge([fx1, fx2], tables, seed=17)
    assert r.status_code == 200, r.text
    out = r.json()["tables"]
    assert out["a"] == [{"id": 1, "name": "aaaaaaaa"},
                        {"id": 2, "name": "dddddddd"}]
    # b's clashing a_id was repicked from the merged a values (only 2 is
    # free); fixture 2's c row followed to that final value
    assert out["b"][0] == {"a_id": 1, "label": "bbbbbbbb"}
    assert out["b"][1] == {"a_id": 2, "label": "eeeeeeee"}
    assert out["c"][0] == {"b_ref": 1, "note": "cccccccc"}
    assert out["c"][1] == {"b_ref": 2, "note": "ffffffff"}


def test_merge_three_level_child_of_deduplicated_parent_stays_put():
    tables = abc_schema()
    fx1 = abc_fx("aaaaaaaa", "bbbbbbbb", "cccccccc", 1)
    fx2 = abc_fx("aaaaaaaa", "bbbbbbbb", "dddddddd", 2)  # same a and b rows
    r = merge([fx1, fx2], tables)
    assert r.status_code == 200, r.text
    out = r.json()["tables"]
    # identical a and b rows collapse to the first copy ...
    assert out["a"] == [{"id": 1, "name": "aaaaaaaa"}]
    assert out["b"] == [{"a_id": 1, "label": "bbbbbbbb"}]
    # ... and both c rows keep pointing at the retained b row
    assert out["c"] == [{"b_ref": 1, "note": "cccccccc"},
                        {"b_ref": 1, "note": "dddddddd"}]


def test_merge_cascade_follows_self_reference_in_middle_table():
    tables = [
        {"name": "p", "rows": 1, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 1000,
                   "unique": True},
            "tag": {"type": "string"}}},
        {"name": "n", "rows": 2, "fields": {
            "pid": {"type": "int", "unique": True}},
         "foreign_keys": [
             {"column": "pid", "references_table": "p", "nullable": False},
             {"column": "parent_pid", "references_table": "n",
              "references_column": "pid", "nullable": True}]},
    ]
    fx1 = {"seed": 1, "row_counts": {"p": 1, "n": 1}, "tables": {
        "p": [{"id": 1, "tag": "aaaaaaaa"}],
        "n": [{"pid": 1, "parent_pid": None}],
    }}
    fx2 = {"seed": 2, "row_counts": {"p": 2, "n": 2}, "tables": {
        "p": [{"id": 1, "tag": "bbbbbbbb"},        # id clash -> rewritten
              {"id": 2, "tag": "cccccccc"}],
        "n": [{"pid": 1, "parent_pid": None},      # follows its p row
              {"pid": 2, "parent_pid": 1}],        # self-ref follows too
    }}
    r = merge([fx1, fx2], tables, seed=19)
    assert r.status_code == 200, r.text
    out = r.json()["tables"]
    new_id = out["p"][1]["id"]
    assert new_id != 1 and out["p"][1]["tag"] == "bbbbbbbb"
    # fixture 2's n rows: pid followed p's rewrite, and the self-reference
    # from the same fixture points at the followed pid, not fixture 1's
    assert out["n"][0] == {"pid": 1, "parent_pid": None}
    assert out["n"][1]["pid"] == new_id
    assert out["n"][2]["pid"] == 2
    assert out["n"][2]["parent_pid"] == new_id


def test_merge_three_level_unresolvable_is_422_without_partial_data():
    tables = [
        {"name": "a", "rows": 1, "fields": {
            "id": {"type": "boolean", "unique": True},
            "name": {"type": "string"}}},
        {"name": "b", "rows": 1, "fields": {
            "a_id": {"type": "boolean", "unique": True}},
         "foreign_keys": [
             {"column": "a_id", "references_table": "a",
              "nullable": False}]},
        {"name": "c", "rows": 1, "fields": {
            "b_ref": {"type": "boolean"}},
         "foreign_keys": [
             {"column": "b_ref", "references_table": "b",
              "references_column": "a_id", "nullable": False}]},
    ]

    def bool_fx(key, name, seed):
        return {"seed": seed,
                "row_counts": {"a": 1, "b": 1, "c": 1},
                "tables": {
                    "a": [{"id": key, "name": name}],
                    "b": [{"a_id": key}],
                    "c": [{"b_ref": key}],
                }}

    # three fixtures but only two boolean values for the unique a.id
    r = merge([bool_fx(True, "aaaaaaaa", 1),
               bool_fx(False, "bbbbbbbb", 2),
               bool_fx(True, "cccccccc", 3)], tables)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unique_space_exhausted"
    assert set(r.json()) == {"detail"}  # no partial merge output


# ------------------------------------------------------------- determinism


def test_merge_same_seed_reproducible_different_seed_may_differ():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}],
             [{"id": 10, "total": 1.5, "user_id": 1}])
    fx2 = fx([{"id": 1, "email": "b@x.example", "name": "C D"}],
             [{"id": 10, "total": 2.5, "user_id": 1}], seed=2)
    r1 = merge([fx1, fx2], tables, seed=55)
    r2 = merge([fx1, fx2], tables, seed=55)
    assert r1.status_code == 200 and r1.json() == r2.json()
    assert any(
        merge([fx1, fx2], tables, seed=s).json() != r1.json()
        for s in range(56, 66)
    )


def test_merge_output_can_be_merged_again():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}],
             [{"id": 10, "total": 1.5, "user_id": 1}])
    fx2 = fx([{"id": 1, "email": "b@x.example", "name": "C D"}],
             [{"id": 10, "total": 2.5, "user_id": 1}], seed=2)
    fx3 = fx([{"id": 2, "email": "c@x.example", "name": "E F"}],
             [{"id": 11, "total": 3.5, "user_id": 2}], seed=3)
    first = merge([fx1, fx2], tables, seed=5)
    assert first.status_code == 200, first.text
    second = merge([first.json(), fx3], tables, seed=6)
    assert second.status_code == 200, second.text
    data = second.json()
    users = data["tables"]["users"]
    assert len(users) == 3
    assert len({u["id"] for u in users}) == 3
    assert len({u["email"] for u in users}) == 3
    ids = {u["id"] for u in users}
    assert all(o["user_id"] in ids for o in data["tables"]["orders"])


def test_merge_two_generated_fixtures_end_to_end():
    tables = [
        {"name": "users", "rows": 5, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 1000,
                   "unique": True},
            "email": {"type": "email", "unique": True}}},
        {"name": "orders", "rows": 10, "fields": {
            "total": {"type": "float"}},
         "foreign_keys": [
             {"column": "user_id", "references_table": "users",
              "nullable": False}]},
    ]
    fx1 = generate(tables, seed=3)
    fx2 = generate(tables, seed=4)
    r = merge([fx1, fx2], tables, seed=9)
    assert r.status_code == 200, r.text
    data = r.json()
    users = data["tables"]["users"]
    orders = data["tables"]["orders"]
    assert len(users) == 10 and len(orders) == 20
    assert len({u["id"] for u in users}) == 10
    assert len({u["email"] for u in users}) == 10
    ids = {u["id"] for u in users}
    assert all(o["user_id"] in ids for o in orders)


# ------------------------------------------------------------- 422 paths


def test_merge_single_fixture_is_422():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}], [])
    fx1["row_counts"] = {"users": 1, "orders": 0}
    assert merge([fx1], tables).status_code == 422
    assert merge([], tables).status_code == 422


def test_merge_fixture_table_set_mismatch_is_422():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}],
             [{"id": 10, "total": 1.5, "user_id": 1}])
    fx2 = {"seed": 2, "row_counts": {"users": 1},
           "tables": {"users": [
               {"id": 2, "email": "b@x.example", "name": "C D"}]}}
    r = merge([fx1, fx2], tables)
    assert r.status_code == 422
    detail = r.json()["detail"][0]
    assert detail["type"] == "fixture_shape_mismatch"
    assert detail["loc"][:3] == ["body", "fixtures", 1]
    assert set(r.json()) == {"detail"}


def test_merge_row_counts_mismatch_is_422():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}],
             [{"id": 10, "total": 1.5, "user_id": 1}])
    fx2 = fx([{"id": 2, "email": "b@x.example", "name": "C D"}],
             [{"id": 11, "total": 2.5, "user_id": 2}], seed=2)
    fx2["row_counts"]["users"] = 99
    r = merge([fx1, fx2], tables)
    assert r.status_code == 422
    detail = r.json()["detail"][0]
    assert detail["type"] == "fixture_shape_mismatch"
    assert detail["loc"][:3] == ["body", "fixtures", 1]


def test_merge_value_type_mismatch_is_422():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}],
             [{"id": 10, "total": 1.5, "user_id": 1}])
    fx2 = fx([{"id": 2, "email": "not-an-email", "name": "C D"}],
             [{"id": 11, "total": 2.5, "user_id": 2}], seed=2)
    r = merge([fx1, fx2], tables)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unsupported_type"


def test_merge_raw_unique_violation_is_422():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}],
             [{"id": 10, "total": 1.5, "user_id": 1}])
    fx2 = fx([{"id": 5, "email": "b@x.example", "name": "C D"},
              {"id": 5, "email": "e@x.example", "name": "E F"}],
             [{"id": 11, "total": 2.5, "user_id": 5}], seed=2)
    r = merge([fx1, fx2], tables)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "fixture_unique_violation"


def test_merge_raw_fk_violation_is_422():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}],
             [{"id": 10, "total": 1.5, "user_id": 1}])
    fx2 = fx([{"id": 2, "email": "b@x.example", "name": "C D"}],
             [{"id": 11, "total": 2.5, "user_id": 999}], seed=2)
    r = merge([fx1, fx2], tables)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "fixture_fk_violation"


def test_merge_unique_space_exhausted_is_422():
    tables = [{"name": "flags", "rows": 2, "fields": {
        "b": {"type": "boolean", "unique": True},
        "tag": {"type": "string"}}}]
    fx1 = {"seed": 1, "row_counts": {"flags": 2}, "tables": {"flags": [
        {"b": True, "tag": "aaaaaaaa"}, {"b": False, "tag": "bbbbbbbb"}]}}
    fx2 = {"seed": 2, "row_counts": {"flags": 2}, "tables": {"flags": [
        {"b": True, "tag": "cccccccc"}, {"b": False, "tag": "dddddddd"}]}}
    r = merge([fx1, fx2], tables)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unique_space_exhausted"
    assert set(r.json()) == {"detail"}


def test_merge_schema_cycle_is_422():
    tables = [
        {"name": "a", "rows": 1, "fields": {"id": {"type": "int", "unique": True}},
         "foreign_keys": [{"column": "b_id", "references_table": "b",
                           "references_column": "id"}]},
        {"name": "b", "rows": 1, "fields": {"id": {"type": "int", "unique": True}},
         "foreign_keys": [{"column": "a_id", "references_table": "a",
                           "references_column": "id"}]},
    ]
    empty = {"seed": 1, "row_counts": {"a": 0, "b": 0},
             "tables": {"a": [], "b": []}}
    r = merge([empty, empty], tables)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "dependency_cycle"
    assert set(r.json()) == {"detail"}


def test_merge_missing_required_body_fields_is_422():
    assert client.post("/fixtures/merge", json={}).status_code == 422
    r = client.post("/fixtures/merge", json={"tables": users_orders_schema()})
    assert r.status_code == 422  # fixtures is required


def test_merge_extra_request_fields_forbidden():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}],
             [{"id": 10, "total": 1.5, "user_id": 1}])
    r = client.post("/fixtures/merge", json={
        "fixtures": [fx1, fx1], "tables": tables, "locale": "zh"})
    assert r.status_code == 422
