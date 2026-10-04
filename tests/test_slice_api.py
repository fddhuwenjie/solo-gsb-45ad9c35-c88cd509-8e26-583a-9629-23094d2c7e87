"""End-to-end tests for the POST /fixtures/slice API."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ------------------------------------------------------------- fixtures


def company_schema():
    """companies -> departments -> employees, plus an unrelated table."""
    return [
        {"name": "companies", "rows": 3, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 1000,
                   "unique": True},
            "name": {"type": "name"}}},
        {"name": "departments", "rows": 5, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 1000,
                   "unique": True}},
         "foreign_keys": [
             {"column": "company_id", "references_table": "companies",
              "nullable": False}]},
        {"name": "employees", "rows": 8, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 1000,
                   "unique": True}},
         "foreign_keys": [
             {"column": "dept_id", "references_table": "departments",
              "nullable": False}]},
        {"name": "audit_log", "rows": 2, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 1000,
                   "unique": True}}},
    ]


def company_fixture():
    return {
        "seed": 1,
        "row_counts": {"companies": 3, "departments": 5, "employees": 8,
                       "audit_log": 2},
        "tables": {
            "companies": [
                {"id": 1, "name": "Acme Corp"},
                {"id": 2, "name": "Beta LLC"},
                {"id": 3, "name": "Cyon Inc"},
            ],
            "departments": [
                {"id": 10, "company_id": 1},
                {"id": 11, "company_id": 1},
                {"id": 20, "company_id": 2},
                {"id": 21, "company_id": 2},
                {"id": 30, "company_id": 3},
            ],
            "employees": [
                {"id": 100, "dept_id": 10},
                {"id": 101, "dept_id": 10},
                {"id": 102, "dept_id": 11},
                {"id": 200, "dept_id": 20},
                {"id": 201, "dept_id": 20},
                {"id": 202, "dept_id": 21},
                {"id": 203, "dept_id": 21},
                {"id": 300, "dept_id": 30},
            ],
            "audit_log": [{"id": 1}, {"id": 2}],
        },
    }


def slice_req(fixture, tables, root, limits=None, seed=7):
    body = {"fixture": fixture, "tables": tables, "root": root, "seed": seed}
    if limits is not None:
        body["limits"] = limits
    return client.post("/fixtures/slice", json=body)


def generate(tables, seed=42):
    r = client.post("/fixtures", json={"tables": tables, "seed": seed})
    assert r.status_code == 200, r.text
    return r.json()


# ----------------------------------------------------------------- closure


def test_slice_basic_closure_and_untouched_tables():
    r = slice_req(company_fixture(), company_schema(),
                  {"table": "companies", "column": "id", "value": 1})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["seed"] == 7
    assert data["row_counts"] == {
        "companies": 1, "departments": 2, "employees": 3, "audit_log": 0,
    }
    tables = data["tables"]
    # rows are copied verbatim, in original fixture order
    assert tables["companies"] == [{"id": 1, "name": "Acme Corp"}]
    assert [d["id"] for d in tables["departments"]] == [10, 11]
    assert [e["id"] for e in tables["employees"]] == [100, 101, 102]
    # untouched table keeps an empty array
    assert tables["audit_log"] == []


def test_slice_from_child_pulls_in_parents_and_grandparents():
    r = slice_req(company_fixture(), company_schema(),
                  {"table": "employees", "column": "id", "value": 100})
    assert r.status_code == 200, r.text
    tables = r.json()["tables"]
    # mandatory chain: employee 100 -> dept 10 -> company 1; the closure then
    # also keeps company 1's other department and its employees
    assert tables["companies"] == [{"id": 1, "name": "Acme Corp"}]
    assert [d["id"] for d in tables["departments"]] == [10, 11]
    assert [e["id"] for e in tables["employees"]] == [100, 101, 102]
    # company 2's subtree stays out
    assert r.json()["row_counts"] == {
        "companies": 1, "departments": 2, "employees": 3, "audit_log": 0,
    }


def test_slice_no_root_match_returns_empty_arrays():
    r = slice_req(company_fixture(), company_schema(),
                  {"table": "companies", "column": "id", "value": 999})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["tables"] == {
        "companies": [], "departments": [], "employees": [], "audit_log": [],
    }
    assert data["row_counts"] == {
        "companies": 0, "departments": 0, "employees": 0, "audit_log": 0,
    }


def test_slice_output_is_directly_runnable_closure():
    # Every non-null FK of every kept row points at a kept row.
    r = slice_req(company_fixture(), company_schema(),
                  {"table": "companies", "column": "id", "value": 2})
    assert r.status_code == 200, r.text
    tables = r.json()["tables"]
    company_ids = {c["id"] for c in tables["companies"]}
    dept_ids = {d["id"] for d in tables["departments"]}
    assert all(d["company_id"] in company_ids for d in tables["departments"])
    assert all(e["dept_id"] in dept_ids for e in tables["employees"])


def test_slice_multiple_root_rows_via_non_unique_column():
    r = slice_req(company_fixture(), company_schema(),
                  {"table": "departments", "column": "company_id", "value": 2})
    assert r.status_code == 200, r.text
    tables = r.json()["tables"]
    assert [d["id"] for d in tables["departments"]] == [20, 21]
    assert [c["id"] for c in tables["companies"]] == [2]
    assert [e["id"] for e in tables["employees"]] == [200, 201, 202, 203]


def test_slice_fixture_row_count_need_not_match_schema_rows():
    # ``rows`` is generation metadata; a fixture may carry any row count.
    tables = company_schema()
    r = slice_req(company_fixture(), tables,
                  {"table": "companies", "column": "id", "value": 1})
    assert r.status_code == 200, r.text
    assert r.json()["row_counts"]["companies"] == 1


def test_slice_keeps_extra_fixture_columns_verbatim():
    tables = [{"name": "t", "rows": 1, "fields": {
        "id": {"type": "int", "min_value": 1, "max_value": 9, "unique": True}}}]
    fx = generate(tables)
    fx["tables"]["t"][0]["extra"] = {"keep": [1, 2]}
    r = slice_req(fx, tables, {"table": "t", "column": "id",
                               "value": fx["tables"]["t"][0]["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["tables"]["t"][0]["extra"] == {"keep": [1, 2]}
    assert "id" in r.json()["tables"]["t"][0]


def test_slice_output_table_order_follows_schema():
    r = slice_req(company_fixture(), company_schema(),
                  {"table": "employees", "column": "id", "value": 100})
    assert r.status_code == 200
    assert list(r.json()["tables"]) == [
        "companies", "departments", "employees", "audit_log",
    ]


def test_slice_null_filter_matches_null_rows():
    tables = [{"name": "nodes", "rows": 3, "fields": {
        "id": {"type": "int", "min_value": 1, "max_value": 100, "unique": True}},
        "foreign_keys": [
            {"column": "parent_id", "references_table": "nodes",
             "nullable": True}]}]
    fx = {"seed": 1, "row_counts": {"nodes": 3}, "tables": {"nodes": [
        {"id": 1, "parent_id": None},
        {"id": 2, "parent_id": 1},
        {"id": 3, "parent_id": 1},
    ]}}
    r = slice_req(fx, tables, {"table": "nodes", "column": "parent_id",
                               "value": None})
    assert r.status_code == 200, r.text
    # root is the parentless row; its children reference it, so all are kept
    assert [n["id"] for n in r.json()["tables"]["nodes"]] == [1, 2, 3]


def test_slice_self_referencing_subtree():
    tables = [{"name": "nodes", "rows": 5, "fields": {
        "id": {"type": "int", "min_value": 1, "max_value": 100, "unique": True}},
        "foreign_keys": [
            {"column": "parent_id", "references_table": "nodes",
             "nullable": True}]}]
    fx = {"seed": 1, "row_counts": {"nodes": 5}, "tables": {"nodes": [
        {"id": 1, "parent_id": None},
        {"id": 2, "parent_id": 1},
        {"id": 3, "parent_id": 2},
        {"id": 4, "parent_id": None},
        {"id": 5, "parent_id": 4},
    ]}}
    r = slice_req(fx, tables, {"table": "nodes", "column": "id", "value": 2})
    assert r.status_code == 200, r.text
    # node 2, its parent 1 and its child 3; the other tree stays out
    assert [n["id"] for n in r.json()["tables"]["nodes"]] == [1, 2, 3]


def test_slice_root_on_synthesized_fk_column():
    # orders.user_id is not declared in fields; it is synthesised from users.id
    tables = [
        {"name": "users", "rows": 2, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 100,
                   "unique": True}}},
        {"name": "orders", "rows": 3, "fields": {
            "total": {"type": "float"}},
         "foreign_keys": [
             {"column": "user_id", "references_table": "users",
              "nullable": False}]},
    ]
    fx = {"seed": 1, "row_counts": {"users": 2, "orders": 3}, "tables": {
        "users": [{"id": 1}, {"id": 2}],
        "orders": [{"total": 1.5, "user_id": 1},
                   {"total": 2.5, "user_id": 2},
                   {"total": 3.5, "user_id": 1}],
    }}
    r = slice_req(fx, tables, {"table": "orders", "column": "user_id",
                               "value": 1})
    assert r.status_code == 200, r.text
    tables_out = r.json()["tables"]
    assert [o["total"] for o in tables_out["orders"]] == [1.5, 3.5]
    assert tables_out["users"] == [{"id": 1}]


def test_slice_works_on_generated_fixture():
    tables = [
        {"name": "users", "rows": 5, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 100000,
                   "unique": True},
            "email": {"type": "email", "unique": True}}},
        {"name": "orders", "rows": 12, "fields": {
            "total": {"type": "float"}},
         "foreign_keys": [
             {"column": "user_id", "references_table": "users",
              "nullable": False}]},
    ]
    fx = generate(tables, seed=3)
    user_id = fx["tables"]["users"][0]["id"]
    r = slice_req(fx, tables, {"table": "users", "column": "id",
                               "value": user_id}, seed=11)
    assert r.status_code == 200, r.text
    out = r.json()["tables"]
    assert out["users"] == [fx["tables"]["users"][0]]
    expected_orders = [o for o in fx["tables"]["orders"]
                       if o["user_id"] == user_id]
    assert out["orders"] == expected_orders


# ------------------------------------------------------------- determinism


def test_slice_same_seed_reproducible_different_seed_may_differ():
    tables = company_schema()
    fx = company_fixture()
    root = {"table": "companies", "column": "id", "value": 1}
    limits = {"departments": 1}
    r1 = slice_req(fx, tables, root, limits=limits, seed=55)
    r2 = slice_req(fx, tables, root, limits=limits, seed=55)
    assert r1.status_code == 200 and r1.json() == r2.json()
    # a different seed picks a different department for the single slot
    other = any(
        slice_req(fx, tables, root, limits=limits, seed=s).json() != r1.json()
        for s in range(56, 66)
    )
    assert other


# ----------------------------------------------------------------- limits


def test_limit_truncates_optional_children_and_slice_stays_closed():
    r = slice_req(company_fixture(), company_schema(),
                  {"table": "companies", "column": "id", "value": 1},
                  limits={"departments": 1})
    assert r.status_code == 200, r.text
    tables = r.json()["tables"]
    assert len(tables["departments"]) == 1
    kept_dept = tables["departments"][0]["id"]
    assert kept_dept in (10, 11)
    # only employees of the kept department survive
    for e in tables["employees"]:
        assert e["dept_id"] == kept_dept
    assert tables["companies"] == [{"id": 1, "name": "Acme Corp"}]


def test_limit_on_root_table_blocking_roots_is_422():
    tables = [{"name": "users", "rows": 3, "fields": {
        "id": {"type": "int", "min_value": 1, "max_value": 100, "unique": True},
        "country": {"type": "enum", "choices": ["CN", "US"]}}}]
    fx = {"seed": 1, "row_counts": {"users": 3}, "tables": {"users": [
        {"id": 1, "country": "CN"},
        {"id": 2, "country": "CN"},
        {"id": 3, "country": "US"},
    ]}}
    r = slice_req(fx, tables, {"table": "users", "column": "country",
                               "value": "CN"}, limits={"users": 1})
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "limit_blocks_closure"
    assert set(r.json()) == {"detail"}


def test_limit_blocking_mandatory_parent_chain_is_422():
    # One payment references two different accounts; its mandatory parent
    # closure needs 2 account rows, so capping accounts at 1 is a 422:
    # parents are never truncated to force the closure.
    tables = [
        {"name": "accounts", "rows": 2, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 100,
                   "unique": True}}},
        {"name": "payments", "rows": 1, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 100,
                    "unique": True}}},
    ]
    # two FKs on the same table point at accounts
    tables[1]["foreign_keys"] = [
        {"column": "from_account_id", "references_table": "accounts",
         "nullable": False},
        {"column": "to_account_id", "references_table": "accounts",
         "nullable": False},
    ]
    fx = {"seed": 1, "row_counts": {"accounts": 2, "payments": 1},
          "tables": {
              "accounts": [{"id": 1}, {"id": 2}],
              "payments": [{"id": 10, "from_account_id": 1,
                             "to_account_id": 2}],
          }}
    root = {"table": "payments", "column": "id", "value": 10}
    r = slice_req(fx, tables, root, limits={"accounts": 1})
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "limit_blocks_closure"
    assert set(r.json()) == {"detail"}
    # uncapped the same request keeps both parents and closes
    r = slice_req(fx, tables, root)
    assert r.status_code == 200, r.text
    assert r.json()["row_counts"] == {"accounts": 2, "payments": 1}


def test_limit_child_forcing_extra_parent_is_422():
    # employee 100 references dept 10 (child of company 1) but company 2;
    # admitting it would force company 2 past the companies cap.
    tables = [
        {"name": "companies", "rows": 2, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 100,
                   "unique": True}}},
        {"name": "departments", "rows": 1, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 100,
                   "unique": True}},
         "foreign_keys": [
             {"column": "company_id", "references_table": "companies",
              "nullable": False}]},
        {"name": "employees", "rows": 1, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 100,
                   "unique": True}},
         "foreign_keys": [
             {"column": "dept_id", "references_table": "departments",
              "nullable": False},
             {"column": "company_id", "references_table": "companies",
              "nullable": False}]},
    ]
    fx = {"seed": 1,
          "row_counts": {"companies": 2, "departments": 1, "employees": 1},
          "tables": {
              "companies": [{"id": 1}, {"id": 2}],
              "departments": [{"id": 10, "company_id": 1}],
              "employees": [{"id": 100, "dept_id": 10, "company_id": 2}],
          }}
    root = {"table": "companies", "column": "id", "value": 1}
    r = slice_req(fx, tables, root, limits={"companies": 1})
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "limit_blocks_closure"
    assert set(r.json()) == {"detail"}
    # without the cap the same slice closes cleanly
    r = slice_req(fx, tables, root)
    assert r.status_code == 200, r.text
    assert r.json()["row_counts"] == {
        "companies": 2, "departments": 1, "employees": 1,
    }


def test_limit_unknown_table_is_422():
    r = slice_req(company_fixture(), company_schema(),
                  {"table": "companies", "column": "id", "value": 1},
                  limits={"ghosts": 1})
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unknown_table"


def test_limit_must_be_positive():
    r = slice_req(company_fixture(), company_schema(),
                  {"table": "companies", "column": "id", "value": 1},
                  limits={"departments": 0})
    assert r.status_code == 422


# ------------------------------------------------------------- 422 paths


def test_root_unknown_table_is_422():
    r = slice_req(company_fixture(), company_schema(),
                  {"table": "ghosts", "column": "id", "value": 1})
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unknown_table"
    assert set(r.json()) == {"detail"}


def test_root_unknown_column_is_422():
    r = slice_req(company_fixture(), company_schema(),
                  {"table": "companies", "column": "nope", "value": 1})
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unknown_column"


def test_root_value_type_mismatch_is_422():
    r = slice_req(company_fixture(), company_schema(),
                  {"table": "companies", "column": "id", "value": "abc"})
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "invalid_filter_value"


def test_raw_fixture_fk_violation_is_422():
    fx = company_fixture()
    fx["tables"]["employees"][0]["dept_id"] = 999
    r = slice_req(fx, company_schema(),
                  {"table": "companies", "column": "id", "value": 1})
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "fixture_fk_violation"
    assert set(r.json()) == {"detail"}


def test_raw_fixture_unique_violation_is_422():
    fx = company_fixture()
    fx["tables"]["companies"][1]["id"] = 1
    r = slice_req(fx, company_schema(),
                  {"table": "companies", "column": "id", "value": 1})
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "fixture_unique_violation"


def test_fixture_shape_mismatch_is_422():
    fx = company_fixture()
    del fx["tables"]["companies"][0]["name"]
    r = slice_req(fx, company_schema(),
                  {"table": "companies", "column": "id", "value": 1})
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "fixture_shape_mismatch"


def test_fixture_table_set_mismatch_is_422():
    fx = company_fixture()
    fx["tables"].pop("audit_log")
    r = slice_req(fx, company_schema(),
                  {"table": "companies", "column": "id", "value": 1})
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "fixture_shape_mismatch"


def test_inconsistent_table_schema_rejected_like_generator():
    tables = [
        {"name": "a", "rows": 1,
         "fields": {"id": {"type": "int", "unique": True}},
         "foreign_keys": [{"column": "b_id", "references_table": "b",
                           "references_column": "id"}]},
        {"name": "b", "rows": 1,
         "fields": {"id": {"type": "int", "unique": True}},
         "foreign_keys": [{"column": "a_id", "references_table": "a",
                           "references_column": "id"}]},
    ]
    fx = {"seed": 1, "row_counts": {"a": 1, "b": 1},
          "tables": {"a": [], "b": []}}
    r = slice_req(fx, tables, {"table": "a", "column": "id", "value": 1})
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "dependency_cycle"
    assert set(r.json()) == {"detail"}


def test_missing_required_body_fields_is_422():
    assert client.post("/fixtures/slice", json={}).status_code == 422
    r = client.post("/fixtures/slice", json={
        "tables": company_schema(), "fixture": company_fixture(),
    })
    assert r.status_code == 422  # root is required


def test_extra_request_fields_forbidden():
    r = client.post("/fixtures/slice", json={
        "fixture": company_fixture(), "tables": company_schema(),
        "root": {"table": "companies", "column": "id", "value": 1},
        "locale": "zh",
    })
    assert r.status_code == 422
