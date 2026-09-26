"""The ten domains: schemas, deterministic generators, contracts and analysis roles.

Every dataset carries the same classes of deliberate anomaly (section 4), and the writer counts
each one it inserts into the manifest, so the benchmark knows the ground truth:

  missing values (per column rate), exact duplicate rows (emitted twice, adjacent), duplicated
  business keys (a row reusing an earlier row's key with different content), numeric outliers
  (x50) and extreme values (1e9), lognormal skew, rare categories (<=0.1%), high-cardinality
  ids, a constant and a near-constant column, correlated / strongly correlated / independent
  measures, class imbalance, dates outside the analysis window, missing dates, 29 February and
  window-edge dates, unsorted dates (every generator draws dates at random), zeros, negatives
  where valid, floats, strings, booleans, timestamps and ids.

A row is a dict of python values; the writer renders them (dates ISO, timestamps
'YYYY-MM-DD HH:MM:SS', booleans true/false, None as an empty field).
"""

from __future__ import annotations

import csv
import datetime as dt
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

D0 = dt.date(2023, 1, 1)


def month_factor(d: dt.date | None, amplitude: float = 0.35) -> float:
    """A yearly cycle peaking in December: the known seasonal pattern."""
    if d is None:
        return 1.0
    return 1.0 + amplitude * math.cos(2 * math.pi * (d.month - 12) / 12)


def rdate(rng: random.Random, start: dt.date = D0, days: int = 731,
          outside: dt.date = dt.date(2025, 1, 1)) -> dt.date:
    """In-window dates, with 1% after the window, 0.1% on 29 February and a few window edges."""
    u = rng.random()
    if u < 0.01:
        return outside + dt.timedelta(days=rng.randrange(28))
    if u < 0.011:
        return dt.date(2024, 2, 29)
    if u < 0.0115:
        return start
    if u < 0.012:
        return start + dt.timedelta(days=days - 1)
    return start + dt.timedelta(days=rng.randrange(days))


def pick(rng: random.Random, values, weights=None):
    return rng.choices(values, weights=weights)[0] if weights else values[rng.randrange(len(values))]


@dataclass
class Domain:
    name: str
    columns: list[str]
    types: dict[str, str]                 # str int float date ts bool
    gen: Callable                         # (rng, i, ctx) -> dict
    business_key: str
    date: str
    window: tuple[str, str]
    measures: dict[str, str]              # column -> agg
    dimensions: list[str]
    roles: dict                           # analysis roles, see matrix.py
    nullable: dict[str, float] = field(default_factory=dict)
    outlier_col: str | None = None
    exclusions: list[dict] = field(default_factory=list)   # {"rule": sql, "reason", "py": callable}
    required: tuple[str, ...] = ()
    edge: tuple[str, ...] = ()
    not_applicable: list[dict] = field(default_factory=list)
    key: str = "record_id"

    def pools(self, n: int) -> dict:
        return {"n": n, "customers": max(200, n // 5), "accounts": max(100, n // 10),
                "people": max(50, n // 20), "small": max(20, n // 100), "mid": max(25, n // 40)}


# --------------------------------------------------------------------------- financial

FIN_CATEGORIES = ["groceries", "rent", "utilities", "travel", "dining", "salary", "insurance",
                  "healthcare", "education", "entertainment", "transfer", "crypto"]
FIN_WEIGHTS = [18, 8, 9, 7, 12, 10, 6, 5, 4, 9, 11.9, 0.1]


def gen_financial(rng, i, ctx):
    d = rdate(rng)
    ttype = pick(rng, ["purchase", "transfer", "refund", "fee", "reversal"], [60, 20, 8, 7, 5])
    revenue = round(rng.lognormvariate(4.0, 1.1) * month_factor(d, 0.2), 2)
    expense = round(revenue * rng.uniform(0.45, 0.75) + rng.gauss(0, 5), 2)
    amount = round(revenue if ttype != "refund" else -revenue, 2)
    debit, credit = (amount, 0.0) if amount >= 0 else (0.0, -amount)
    budget = round(rng.uniform(50, 400), 2)
    risk = round(min(1.0, max(0.0, rng.betavariate(2, 8))), 4)
    fraud = rng.random() < (0.004 + 0.12 * max(0.0, risk - 0.5))
    return {
        "transaction_id": f"T{i:08d}", "account_id": f"A{rng.randrange(ctx['accounts']):07d}",
        "customer_id": f"C{rng.randrange(ctx['customers']):07d}", "transaction_date": d,
        "posted_at": dt.datetime.combine(d, dt.time(rng.randrange(24), rng.randrange(60),
                                                   rng.randrange(60))),
        "transaction_type": ttype,
        "category": pick(rng, FIN_CATEGORIES, FIN_WEIGHTS),
        "debit": debit, "credit": credit, "amount": amount,
        "balance": round(rng.gauss(2500, 4000), 2),
        "currency": "USD" if rng.random() < 0.999 else "EUR",
        "branch": f"BR{rng.randrange(40):02d}",
        "payment_method": pick(rng, ["card", "ach", "wire", "cash", "cheque"], [55, 25, 8, 10, 2]),
        "risk_score": risk, "fraud_flag": fraud, "revenue": revenue, "expense": expense,
        "profit": round(revenue - expense, 2), "budget": budget,
        "actual": round(budget * rng.uniform(0.7, 1.3), 2),
        "fiscal_period": f"FY{d.year}-Q{(d.month - 1) // 3 + 1}",
        "source_system": "CORE",
    }


FINANCIAL = Domain(
    name="financial",
    columns=["record_id", "transaction_id", "account_id", "customer_id", "transaction_date",
             "posted_at", "transaction_type", "category", "debit", "credit", "amount", "balance",
             "currency", "branch", "payment_method", "risk_score", "fraud_flag", "revenue",
             "expense", "profit", "budget", "actual", "fiscal_period", "source_system"],
    types={"record_id": "str", "transaction_id": "str", "account_id": "str", "customer_id": "str",
           "transaction_date": "date", "posted_at": "ts", "transaction_type": "str",
           "category": "str", "debit": "float", "credit": "float", "amount": "float",
           "balance": "float", "currency": "str", "branch": "str", "payment_method": "str",
           "risk_score": "float", "fraud_flag": "bool", "revenue": "float", "expense": "float",
           "profit": "float", "budget": "float", "actual": "float", "fiscal_period": "str",
           "source_system": "str"},
    gen=gen_financial, business_key="transaction_id", date="transaction_date",
    window=("2023-01-01", "2024-12-31"),
    measures={"revenue": "sum", "expense": "sum", "profit": "sum", "amount": "sum",
              "budget": "sum", "actual": "sum", "debit": "sum", "credit": "sum",
              "risk_score": "mean", "balance": "none"},
    dimensions=["transaction_type", "category", "payment_method", "branch", "currency",
                "fraud_flag", "fiscal_period", "customer_id", "account_id"],
    roles=dict(dims_low=["payment_method", "transaction_type"], dim_mid="branch",
               dim_hi="customer_id", binary="fraud_flag", m_sum="revenue", m_sum2="expense",
               m_mean="risk_score", m_x="revenue", m_y="expense", m_indep="budget",
               entity="customer_id", m_none="balance", dim_const="currency"),
    nullable={"category": 0.01, "payment_method": 0.005, "risk_score": 0.02, "revenue": 0.005,
              "transaction_date": 0.005, "branch": 0.002},
    outlier_col="revenue",
    exclusions=[{"rule": "transaction_type = 'reversal'",
                 "reason": "reversals cancel an earlier transaction",
                 "columns": ["transaction_type"],
                 "py": lambda r: r.get("transaction_type") == "reversal"}],
    required=("summary_stats", "top_n", "trend", "period_compare", "concentration",
              "outlier_detection", "correlation", "distribution", "frequency", "pareto"),
    not_applicable=[
        {"analysis": "correlation", "params": {"measure": "revenue", "against": "category"},
         "expect": "reject", "why": "category is text, not a measure"},
        {"analysis": "trend", "params": {"measure": "balance", "grain": "month"},
         "expect": "reject", "why": "balance is non-additive (agg none)"},
        {"analysis": "pareto", "params": {"dimension": "branch", "measure": "risk_score"},
         "expect": "reject", "why": "a mean has no share"},
    ],
)

# --------------------------------------------------------------------------- HR


def gen_hr(rng, i, ctx):
    hire = rdate(rng, dt.date(2015, 1, 1), 3653, dt.date(2025, 1, 1))
    if rng.random() < 0.2:
        hire = dt.date(2008, 1, 1) + dt.timedelta(days=rng.randrange(2500))   # before the window
    age = rng.randint(20, 64)
    exp = max(0, min(age - 18, int(rng.gauss((age - 20) * 0.8, 3))))
    status = pick(rng, ["active", "terminated", "on_leave"], [80, 17, 3])
    term = (hire + dt.timedelta(days=rng.randrange(30, 3000))) if status == "terminated" else None
    tenure = round(((term or dt.date(2025, 1, 1)) - hire).days / 365.25, 2)
    perf = round(min(5.0, max(1.0, rng.gauss(3.4, 0.7))), 2)
    salary = round(rng.lognormvariate(11.0, 0.35) * (1 + exp / 40), 2)
    dept = pick(rng, ["engineering", "sales", "support", "finance", "hr", "legal", "marketing",
                      "operations", "research", "security"], [22, 18, 16, 8, 5, 3, 9, 12, 5, 2])
    return {
        "employee_id": f"E{i:07d}", "department": dept, "team": f"{dept[:3]}-{rng.randrange(6)}",
        "job_role": f"role_{rng.randrange(30):02d}", "hire_date": hire, "termination_date": term,
        "employment_status": status, "salary": salary,
        "bonus": round(salary * max(0.0, (perf - 2.5)) * 0.04, 2), "age": age,
        "years_experience": exp, "tenure": tenure, "performance_score": perf,
        "attendance_rate": round(min(1.0, max(0.6, rng.gauss(0.95, 0.04))), 4),
        "overtime_hours": 0 if rng.random() < 0.45 else round(rng.expovariate(1 / 12), 1),
        "training_hours": round(rng.uniform(0, 80), 1), "promotion_count": min(6, int(tenure / 3)),
        "manager_id": f"M{rng.randrange(ctx['accounts']):06d}",
        "location": pick(rng, ["NYC", "SFO", "AUS", "CHI", "LON", "BLR", "BER", "TOR"]),
        "attrition_flag": status == "terminated", "country": "US" if rng.random() < 0.998 else "UK",
    }


HR = Domain(
    name="hr",
    columns=["record_id", "employee_id", "department", "team", "job_role", "hire_date",
             "termination_date", "employment_status", "salary", "bonus", "age",
             "years_experience", "tenure", "performance_score", "attendance_rate",
             "overtime_hours", "training_hours", "promotion_count", "manager_id", "location",
             "attrition_flag", "country"],
    types={"record_id": "str", "employee_id": "str", "department": "str", "team": "str",
           "job_role": "str", "hire_date": "date", "termination_date": "date",
           "employment_status": "str", "salary": "float", "bonus": "float", "age": "int",
           "years_experience": "int", "tenure": "float", "performance_score": "float",
           "attendance_rate": "float", "overtime_hours": "float", "training_hours": "float",
           "promotion_count": "int", "manager_id": "str", "location": "str",
           "attrition_flag": "bool", "country": "str"},
    gen=gen_hr, business_key="employee_id", date="hire_date",
    window=("2015-01-01", "2024-12-31"),
    measures={"salary": "sum", "bonus": "sum", "overtime_hours": "sum", "training_hours": "sum",
              "promotion_count": "sum", "performance_score": "mean", "attendance_rate": "mean",
              "age": "mean", "tenure": "mean", "years_experience": "mean"},
    dimensions=["department", "team", "job_role", "employment_status", "location",
                "attrition_flag", "country", "manager_id", "employee_id"],
    roles=dict(dims_low=["department", "location"], dim_mid="team", dim_hi="manager_id",
               binary="attrition_flag", m_sum="salary", m_sum2="bonus",
               m_mean="performance_score", m_x="age", m_y="years_experience",
               m_indep="training_hours", entity="employee_id", dim_const="country"),
    nullable={"performance_score": 0.03, "department": 0.004, "hire_date": 0.005,
              "salary": 0.003, "location": 0.002},
    outlier_col="salary",
    required=("summary_stats", "group_compare", "distribution", "correlation",
              "hypothesis_test", "outlier_detection", "trend", "frequency"),
    edge=("repeat_behaviour", "cohort_retention"),
    not_applicable=[
        {"analysis": "repeat_behaviour", "params": {"entity": "employee_id"},
         "expect": "trivial", "why": "one row per employee: everybody occurs once"},
        {"analysis": "correlation", "params": {"measure": "salary", "against": "department"},
         "expect": "reject", "why": "department is text"},
        {"analysis": "growth_decomposition",
         "params": {"measure": "performance_score", "dimension": "department",
                    "period": "2024-12", "baseline": "2024-11", "grain": "month"},
         "expect": "reject", "why": "a mean does not split across members"},
    ],
)

# --------------------------------------------------------------------------- sales

SALES_CATS = ["electronics", "apparel", "home", "beauty", "sports", "toys", "books", "grocery",
              "collectibles"]


def gen_sales(rng, i, ctx):
    d = rdate(rng)
    q = max(1, int(rng.expovariate(1 / 4) * month_factor(d)) + 1)
    price = round(rng.lognormvariate(3.2, 0.8), 2)
    disc = 0.0 if rng.random() < 0.6 else round(rng.uniform(0.05, 0.4), 2)
    gross = round(q * price, 2)
    net = round(gross * (1 - disc), 2)
    cost = round(gross * rng.uniform(0.55, 0.8), 2)
    return {
        "order_id": f"SO{i:08d}", "customer_id": f"C{rng.randrange(ctx['customers']):07d}",
        "salesperson_id": f"SP{rng.randrange(ctx['small']):05d}",
        "product_id": f"P{rng.randrange(ctx['mid']):06d}",
        "product_category": pick(rng, SALES_CATS, [20, 18, 15, 10, 10, 8, 9, 9.9, 0.1]),
        "region": pick(rng, ["north", "south", "east", "west", "central"], [30, 25, 20, 15, 10]),
        "city": f"city_{rng.randrange(60):02d}", "order_date": d,
        "quantity": q, "unit_price": price, "discount": disc, "gross_sales": gross,
        "net_sales": net, "cost": cost, "profit": round(net - cost, 2),
        "channel": pick(rng, ["online", "store", "partner"], [55, 35, 10]),
        "returned_flag": rng.random() < 0.05,
        "shipped_at": dt.datetime.combine(d, dt.time(rng.randrange(24), rng.randrange(60))),
    }


SALES = Domain(
    name="sales",
    columns=["record_id", "order_id", "customer_id", "salesperson_id", "product_id",
             "product_category", "region", "city", "order_date", "quantity", "unit_price",
             "discount", "gross_sales", "net_sales", "cost", "profit", "channel",
             "returned_flag", "shipped_at"],
    types={"record_id": "str", "order_id": "str", "customer_id": "str", "salesperson_id": "str",
           "product_id": "str", "product_category": "str", "region": "str", "city": "str",
           "order_date": "date", "quantity": "int", "unit_price": "float", "discount": "float",
           "gross_sales": "float", "net_sales": "float", "cost": "float", "profit": "float",
           "channel": "str", "returned_flag": "bool", "shipped_at": "ts"},
    gen=gen_sales, business_key="order_id", date="order_date", window=("2023-01-01", "2024-12-31"),
    measures={"quantity": "sum", "gross_sales": "sum", "net_sales": "sum", "cost": "sum",
              "profit": "sum", "discount": "mean", "unit_price": "none"},
    dimensions=["product_category", "region", "city", "channel", "returned_flag",
                "customer_id", "salesperson_id", "product_id"],
    roles=dict(dims_low=["region", "channel"], dim_mid="city", dim_hi="customer_id",
               binary="returned_flag", m_sum="net_sales", m_sum2="quantity", m_mean="discount",
               m_x="gross_sales", m_y="net_sales", m_indep="discount", entity="customer_id",
               m_none="unit_price"),
    nullable={"product_category": 0.005, "region": 0.003, "order_date": 0.005, "net_sales": 0.002,
              "discount": 0.01},
    outlier_col="net_sales",
    required=("trend", "top_n", "pareto", "concentration", "mix_shift", "growth_decomposition",
              "period_compare", "seasonality", "ranking_shift", "correlation"),
    not_applicable=[
        {"analysis": "correlation", "params": {"measure": "net_sales", "against": "region"},
         "expect": "reject", "why": "region is text"},
        {"analysis": "top_n", "params": {"dimension": "region", "measure": "unit_price"},
         "expect": "reject", "why": "unit_price is non-additive"},
    ],
)

# --------------------------------------------------------------------------- marketing


def gen_marketing(rng, i, ctx):
    d = rdate(rng)
    impressions = int(rng.lognormvariate(8, 1.2))
    ctr_true = rng.uniform(0.005, 0.06)
    clicks = int(impressions * ctr_true)
    leads = int(clicks * rng.uniform(0.02, 0.2))
    conversions = int(leads * rng.uniform(0.1, 0.5))
    spend = round(clicks * rng.uniform(0.3, 2.5) + rng.uniform(0, 20), 2)
    revenue = round(conversions * rng.lognormvariate(4, 0.6), 2)
    return {
        "campaign_id": f"CMP{rng.randrange(ctx['mid']):05d}",
        "channel": pick(rng, ["search", "social", "display", "email", "video", "affiliate"],
                        [30, 25, 15, 15, 10, 5]),
        "campaign_type": pick(rng, ["awareness", "consideration", "conversion", "retention"]),
        "date": d, "impressions": impressions, "clicks": clicks, "leads": leads,
        "conversions": conversions, "spend": spend, "revenue": revenue,
        "ctr": round(clicks / impressions, 5) if impressions else None,
        "cpc": round(spend / clicks, 4) if clicks else None,
        "cpl": round(spend / leads, 4) if leads else None,
        "conversion_rate": round(conversions / clicks, 5) if clicks else None,
        "roas": round(revenue / spend, 4) if spend else None,
        "audience_segment": f"seg_{rng.randrange(8)}", "device": pick(rng, ["mobile", "desktop",
                                                                          "tablet"], [60, 35, 5]),
        "geography": pick(rng, [f"geo_{k:02d}" for k in range(20)] + ["antarctica"],
                          [5] * 20 + [0.05]),
        "retargeting_flag": rng.random() < 0.3,
    }


MARKETING = Domain(
    name="marketing",
    columns=["record_id", "campaign_row_id", "campaign_id", "channel", "campaign_type", "date",
             "impressions", "clicks", "leads", "conversions", "spend", "revenue", "ctr", "cpc",
             "cpl", "conversion_rate", "roas", "audience_segment", "device", "geography", "retargeting_flag"],
    types={"record_id": "str", "campaign_row_id": "str", "campaign_id": "str", "channel": "str",
           "campaign_type": "str", "date": "date", "impressions": "int", "clicks": "int",
           "leads": "int", "conversions": "int", "spend": "float", "revenue": "float",
           "ctr": "float", "cpc": "float", "cpl": "float", "conversion_rate": "float",
           "roas": "float", "audience_segment": "str", "device": "str", "geography": "str",
           "retargeting_flag": "bool"},
    gen=lambda rng, i, ctx: {"campaign_row_id": f"MR{i:08d}", **gen_marketing(rng, i, ctx)},
    business_key="campaign_row_id", date="date", window=("2023-01-01", "2024-12-31"),
    measures={"impressions": "sum", "clicks": "sum", "leads": "sum", "conversions": "sum",
              "spend": "sum", "revenue": "sum", "ctr": "mean", "roas": "none", "cpc": "none",
              "conversion_rate": "none"},
    dimensions=["channel", "campaign_type", "audience_segment", "device", "geography",
                "retargeting_flag", "campaign_id"],
    roles=dict(dims_low=["channel", "device"], dim_mid="geography", dim_hi="campaign_id",
               binary="retargeting_flag", m_sum="revenue", m_sum2="spend", m_mean="ctr",
               m_x="spend", m_y="clicks", m_indep="ctr", entity="campaign_id", m_none="roas"),
    nullable={"channel": 0.004, "date": 0.005, "revenue": 0.003},
    outlier_col="spend",
    required=("group_compare", "correlation", "trend", "mix_shift", "hypothesis_test",
              "top_n", "summary_stats"),
    not_applicable=[
        {"analysis": "trend", "params": {"measure": "roas", "grain": "month"},
         "expect": "reject", "why": "roas is a ratio declared non-additive"},
        {"analysis": "correlation", "params": {"measure": "spend", "against": "channel"},
         "expect": "reject", "why": "channel is text"},
    ],
)

# --------------------------------------------------------------------------- CRM


def gen_crm(rng, i, ctx):
    signup = rdate(rng, dt.date(2022, 1, 1), 1096)
    orders = 0 if rng.random() < 0.15 else int(rng.expovariate(1 / 6)) + 1
    spend = round(sum(rng.lognormvariate(3.8, 0.7) for _ in range(min(orders, 40))), 2)
    last = (signup + dt.timedelta(days=rng.randrange(1, 700))) if orders else None
    tickets = int(rng.expovariate(1 / 1.5))
    sat = round(min(10.0, max(1.0, rng.gauss(7.5 - 0.4 * tickets, 1.5))), 1)
    return {
        "customer_id": f"CU{i:08d}", "signup_date": signup, "last_purchase_date": last,
        "region": pick(rng, ["na", "emea", "apac", "latam"], [45, 30, 20, 5]),
        "segment": pick(rng, ["consumer", "smb", "enterprise"], [70, 25, 5]),
        "acquisition_channel": pick(rng, ["organic", "paid_search", "referral", "social",
                                          "partner"]),
        "orders_count": orders, "total_spend": spend,
        "average_order_value": round(spend / orders, 2) if orders else None,
        "support_tickets": tickets, "complaints": min(tickets, int(rng.expovariate(2))),
        "satisfaction_score": sat,
        "last_interaction_date": signup + dt.timedelta(days=rng.randrange(0, 900)),
        "churn_flag": rng.random() < (0.1 + 0.03 * tickets),
        "loyalty_status": pick(rng, ["bronze", "silver", "gold", "platinum"],
                               [60, 28, 11.9, 0.1]),
    }


CRM = Domain(
    name="crm",
    columns=["record_id", "customer_id", "signup_date", "last_purchase_date", "region",
             "segment", "acquisition_channel", "orders_count", "total_spend",
             "average_order_value", "support_tickets", "complaints", "satisfaction_score",
             "last_interaction_date", "churn_flag", "loyalty_status"],
    types={"record_id": "str", "customer_id": "str", "signup_date": "date",
           "last_purchase_date": "date", "region": "str", "segment": "str",
           "acquisition_channel": "str", "orders_count": "int", "total_spend": "float",
           "average_order_value": "float", "support_tickets": "int", "complaints": "int",
           "satisfaction_score": "float", "last_interaction_date": "date", "churn_flag": "bool",
           "loyalty_status": "str"},
    gen=gen_crm, business_key="customer_id", date="signup_date",
    window=("2022-01-01", "2024-12-31"),
    measures={"total_spend": "sum", "orders_count": "sum", "support_tickets": "sum",
              "complaints": "sum", "satisfaction_score": "mean", "average_order_value": "none"},
    dimensions=["region", "segment", "acquisition_channel", "churn_flag", "loyalty_status",
                "customer_id"],
    roles=dict(dims_low=["segment", "region"], dim_mid="acquisition_channel",
               dim_hi="customer_id", binary="churn_flag", m_sum="total_spend",
               m_sum2="orders_count", m_mean="satisfaction_score", m_x="orders_count",
               m_y="total_spend", m_indep="satisfaction_score", entity="customer_id",
               m_none="average_order_value"),
    nullable={"region": 0.004, "satisfaction_score": 0.05, "signup_date": 0.005,
              "total_spend": 0.002},
    outlier_col="total_spend",
    required=("frequency", "top_n", "pareto", "concentration", "group_compare",
              "distribution", "hypothesis_test"),
    edge=("repeat_behaviour", "cohort_retention"),
    not_applicable=[
        {"analysis": "pareto", "params": {"dimension": "customer_id", "measure": "total_spend"},
         "expect": "reject", "why": "a customer per row exceeds the group cap"},
        {"analysis": "trend", "params": {"measure": "average_order_value", "grain": "month"},
         "expect": "reject", "why": "average_order_value is non-additive"},
    ],
)

# --------------------------------------------------------------------------- e-commerce


def gen_ecommerce(rng, i, ctx):
    d = rdate(rng)
    cancelled = rng.random() < 0.04
    shipped = None if cancelled else d + dt.timedelta(days=rng.randrange(0, 4))
    delivered = None if (cancelled or rng.random() < 0.03) else shipped + dt.timedelta(
        days=max(1, int(rng.expovariate(1 / 3))))
    q = max(1, int(rng.expovariate(1 / 1.6)))
    price = round(rng.lognormvariate(3.0, 0.9), 2)
    ship = 0.0 if rng.random() < 0.3 else round(rng.uniform(2, 25), 2)
    disc = 0.0 if rng.random() < 0.7 else round(q * price * rng.uniform(0.05, 0.3), 2)
    return {
        "order_id": f"EO{i:08d}", "customer_id": f"C{rng.randrange(ctx['customers']):07d}",
        "product_id": f"P{rng.randrange(ctx['mid']):06d}",
        "seller_id": f"S{rng.randrange(ctx['small']):05d}",
        "category": pick(rng, ["fashion", "electronics", "home", "beauty", "toys", "garden",
                               "auto", "rare_vinyl"], [22, 20, 18, 12, 10, 9, 8.9, 0.1]),
        "order_date": d, "shipped_date": shipped, "delivered_date": delivered, "quantity": q,
        "item_price": price, "shipping_cost": ship, "discount": disc,
        "payment_type": pick(rng, ["card", "wallet", "cod", "voucher"], [55, 25, 15, 5]),
        "payment_value": round(q * price + ship - disc, 2),
        "review_score": None if rng.random() < 0.3 else pick(rng, [1, 2, 3, 4, 5],
                                                             [8, 6, 12, 30, 44]),
        "returned_flag": rng.random() < 0.06, "cancelled_flag": cancelled,
        "device": pick(rng, ["mobile", "desktop", "app"], [50, 30, 20]),
        "traffic_source": pick(rng, ["direct", "search", "social", "email", "ads"]),
        "ordered_at": dt.datetime.combine(d, dt.time(rng.randrange(24), rng.randrange(60))),
    }


ECOMMERCE = Domain(
    name="ecommerce",
    columns=["record_id", "order_id", "customer_id", "product_id", "seller_id", "category",
             "order_date", "shipped_date", "delivered_date", "quantity", "item_price",
             "shipping_cost", "discount", "payment_type", "payment_value", "review_score",
             "returned_flag", "cancelled_flag", "device", "traffic_source", "ordered_at"],
    types={"record_id": "str", "order_id": "str", "customer_id": "str", "product_id": "str",
           "seller_id": "str", "category": "str", "order_date": "date", "shipped_date": "date",
           "delivered_date": "date", "quantity": "int", "item_price": "float",
           "shipping_cost": "float", "discount": "float", "payment_type": "str",
           "payment_value": "float", "review_score": "int", "returned_flag": "bool",
           "cancelled_flag": "bool", "device": "str", "traffic_source": "str",
           "ordered_at": "ts"},
    gen=gen_ecommerce, business_key="order_id", date="order_date",
    window=("2023-01-01", "2024-12-31"),
    measures={"quantity": "sum", "payment_value": "sum", "shipping_cost": "sum",
              "discount": "sum", "review_score": "mean", "item_price": "none"},
    dimensions=["category", "payment_type", "device", "traffic_source", "returned_flag",
                "cancelled_flag", "customer_id", "product_id", "seller_id"],
    roles=dict(dims_low=["category", "payment_type"], dim_mid="traffic_source",
               dim_hi="customer_id", binary="returned_flag", m_sum="payment_value",
               m_sum2="quantity", m_mean="review_score", m_x="quantity", m_y="payment_value",
               m_indep="review_score", entity="customer_id", m_none="item_price"),
    nullable={"category": 0.004, "order_date": 0.005, "payment_value": 0.002,
              "payment_type": 0.003},
    outlier_col="payment_value",
    required=("trend", "top_n", "pareto", "repeat_behaviour", "cohort_retention", "seasonality",
              "frequency"),
    not_applicable=[
        {"analysis": "correlation", "params": {"measure": "payment_value", "against": "device"},
         "expect": "reject", "why": "device is text"},
    ],
)

# --------------------------------------------------------------------------- logistics


def gen_logistics(rng, i, ctx):
    d = rdate(rng)
    promised = d + dt.timedelta(days=rng.randrange(1, 6))
    status = pick(rng, ["delivered", "rto", "in_transit", "lost"], [85, 9, 5.9, 0.1])
    dist = round(rng.lognormvariate(3.3, 0.8), 1)
    hours = round(max(1.0, dist / rng.uniform(15, 40) * 3 + rng.expovariate(1 / 8)), 2)
    delivered = (dt.datetime.combine(d, dt.time(8)) + dt.timedelta(hours=hours)
                 if status == "delivered" else None)
    attempts = 1 if rng.random() < 0.8 else rng.randint(2, 4)
    fake = rng.random() < 0.015
    return {
        "shipment_id": f"SH{i:09d}", "order_id": f"O{rng.randrange(ctx['n'] * 2):09d}",
        "customer_id": f"C{rng.randrange(ctx['customers']):07d}",
        "hub_id": f"HUB{rng.randrange(30):02d}", "rider_id": f"R{rng.randrange(ctx['people']):06d}",
        "origin_city": f"oc_{rng.randrange(25):02d}", "destination_city": f"dc_{rng.randrange(40):02d}",
        "zone": pick(rng, ["local", "metro", "regional", "national", "remote"], [30, 30, 20, 15, 5]),
        "shipment_date": d, "promised_date": promised,
        "delivery_date": delivered.date() if delivered else None,
        "status": status, "attempts": attempts + (1 if fake else 0), "distance_km": dist,
        "delivery_time_hours": hours if status == "delivered" else None,
        "cod_amount": 0.0 if rng.random() < 0.55 else round(rng.lognormvariate(4, 0.9), 2),
        "weight_kg": round(rng.lognormvariate(0.5, 0.9), 3),
        "sla_breach": bool(delivered and delivered.date() > promised),
        "fake_attempt_flag": fake,
        "ndr_reason": None if attempts == 1 and not fake else pick(
            rng, ["customer_unavailable", "address_issue", "refused", "cod_not_ready"]),
    }


LOGISTICS = Domain(
    name="logistics",
    columns=["record_id", "shipment_id", "order_id", "customer_id", "hub_id", "rider_id",
             "origin_city", "destination_city", "zone", "shipment_date", "promised_date",
             "delivery_date", "status", "attempts", "distance_km", "delivery_time_hours",
             "cod_amount", "weight_kg", "sla_breach", "fake_attempt_flag", "ndr_reason"],
    types={"record_id": "str", "shipment_id": "str", "order_id": "str", "customer_id": "str",
           "hub_id": "str", "rider_id": "str", "origin_city": "str", "destination_city": "str",
           "zone": "str", "shipment_date": "date", "promised_date": "date",
           "delivery_date": "date", "status": "str", "attempts": "int", "distance_km": "float",
           "delivery_time_hours": "float", "cod_amount": "float", "weight_kg": "float",
           "sla_breach": "bool", "fake_attempt_flag": "bool", "ndr_reason": "str"},
    gen=gen_logistics, business_key="shipment_id", date="shipment_date",
    window=("2023-01-01", "2024-12-31"),
    measures={"cod_amount": "sum", "distance_km": "sum", "weight_kg": "sum", "attempts": "sum",
              "delivery_time_hours": "mean"},
    dimensions=["hub_id", "zone", "status", "origin_city", "destination_city", "sla_breach",
                "fake_attempt_flag", "ndr_reason", "rider_id", "customer_id"],
    roles=dict(dims_low=["zone", "status"], dim_mid="hub_id", dim_hi="rider_id",
               binary="sla_breach", m_sum="cod_amount", m_sum2="distance_km",
               m_mean="delivery_time_hours", m_x="distance_km", m_y="delivery_time_hours",
               m_indep="weight_kg", entity="customer_id"),
    nullable={"hub_id": 0.003, "shipment_date": 0.005, "weight_kg": 0.004},
    outlier_col="cod_amount",
    required=("group_compare", "top_n", "distribution", "outlier_detection", "trend",
              "concentration", "period_compare", "frequency"),
    not_applicable=[
        {"analysis": "correlation", "params": {"measure": "distance_km", "against": "zone"},
         "expect": "reject", "why": "zone is text"},
    ],
)

# --------------------------------------------------------------------------- healthcare (synthetic)


def gen_healthcare(rng, i, ctx):
    d = rdate(rng)
    group = pick(rng, ["A", "B"])
    severity = rng.randint(1, 10)
    los = max(0, int(rng.expovariate(1 / (3 + severity * 0.6 - (1.0 if group == "A" else 0)))))
    return {
        "admission_id": f"AD{i:09d}", "patient_id": f"PT{rng.randrange(ctx['customers']):07d}",
        "doctor_id": f"DR{rng.randrange(ctx['small']):05d}",
        "hospital_id": f"H{rng.randrange(12):02d}", "admission_date": d,
        "discharge_date": d + dt.timedelta(days=los), "age": rng.randint(0, 99),
        "sex": pick(rng, ["F", "M", "U"], [50, 49.9, 0.1]),
        "diagnosis_group": f"dx_{rng.randrange(15):02d}", "treatment_group": group,
        "lab_value": round(rng.gauss(5.5 + (0.3 if group == "B" else 0), 1.2), 3),
        "severity_score": severity, "length_of_stay": los,
        "readmission_flag": rng.random() < 0.08, "followup_flag": rng.random() < 0.6,
        "medication_count": rng.randint(0, 12),
        "treatment_cost": round(rng.lognormvariate(8.5, 0.9) * (1 + los / 10), 2),
        "outcome": pick(rng, ["recovered", "improved", "unchanged", "deceased"],
                        [60, 28, 11.8, 0.2]),
    }


HEALTHCARE = Domain(
    name="healthcare",
    columns=["record_id", "admission_id", "patient_id", "doctor_id", "hospital_id",
             "admission_date", "discharge_date", "age", "sex", "diagnosis_group",
             "treatment_group", "lab_value", "severity_score", "length_of_stay",
             "readmission_flag", "followup_flag", "medication_count", "treatment_cost",
             "outcome"],
    types={"record_id": "str", "admission_id": "str", "patient_id": "str", "doctor_id": "str",
           "hospital_id": "str", "admission_date": "date", "discharge_date": "date",
           "age": "int", "sex": "str", "diagnosis_group": "str", "treatment_group": "str",
           "lab_value": "float", "severity_score": "int", "length_of_stay": "int",
           "readmission_flag": "bool", "followup_flag": "bool", "medication_count": "int",
           "treatment_cost": "float", "outcome": "str"},
    gen=gen_healthcare, business_key="admission_id", date="admission_date",
    window=("2023-01-01", "2024-12-31"),
    measures={"treatment_cost": "sum", "medication_count": "sum", "length_of_stay": "mean",
              "lab_value": "mean", "severity_score": "mean", "age": "mean"},
    dimensions=["hospital_id", "diagnosis_group", "treatment_group", "sex", "outcome",
                "readmission_flag", "followup_flag", "doctor_id", "patient_id"],
    roles=dict(dims_low=["hospital_id", "outcome"], dim_mid="diagnosis_group",
               dim_hi="doctor_id", binary="treatment_group", m_sum="treatment_cost",
               m_sum2="medication_count", m_mean="length_of_stay", m_x="length_of_stay",
               m_y="treatment_cost", m_indep="lab_value", entity="patient_id"),
    nullable={"lab_value": 0.03, "admission_date": 0.005, "treatment_cost": 0.003},
    outlier_col="treatment_cost",
    required=("hypothesis_test", "effect_size", "confidence_interval", "group_compare",
              "distribution", "correlation", "outlier_detection", "cohort_retention"),
    not_applicable=[
        {"analysis": "correlation", "params": {"measure": "lab_value", "against": "sex"},
         "expect": "reject", "why": "sex is text"},
    ],
)

# --------------------------------------------------------------------------- manufacturing


def gen_manufacturing(rng, i, ctx):
    d = rdate(rng)
    runtime = round(rng.uniform(300, 480), 1)
    downtime = 0.0 if rng.random() < 0.5 else round(rng.expovariate(1 / 25), 1)
    units = int(runtime * rng.uniform(1.5, 2.5))
    vibration = round(rng.lognormvariate(0.5, 0.4), 3)
    shift_after = d is not None and d >= dt.date(2024, 7, 1)       # the known changepoint
    rate = min(0.5, max(0.0, 0.01 + 0.01 * vibration + (0.02 if shift_after else 0)
                        + rng.gauss(0, 0.005)))
    defective = int(units * rate)
    maint = rng.random() < 0.08
    return {
        "production_id": f"PR{i:09d}", "plant_id": f"PL{rng.randrange(6)}",
        "machine_id": f"MC{rng.randrange(ctx['mid']):05d}", "product_id": f"SKU{rng.randrange(30):02d}",
        "shift": pick(rng, ["A", "B", "C"], [40, 35, 25]),
        "operator_id": f"OP{rng.randrange(ctx['people']):06d}", "production_date": d,
        "units_produced": units, "defective_units": defective,
        "defect_rate": round(defective / units, 5) if units else None,
        "downtime_minutes": downtime, "runtime_minutes": runtime,
        "temperature": round(rng.gauss(-5 if rng.random() < 0.1 else 65, 8), 2),
        "pressure": round(rng.gauss(101.3, 2.5), 2), "vibration": vibration,
        "energy_usage": round(runtime * 0.8 + rng.gauss(0, 10), 2), "maintenance_flag": maint,
        "maintenance_date": d + dt.timedelta(days=rng.randrange(1, 30)) if maint else None,
        "batch_id": f"B{rng.randrange(ctx['n'] // 3 + 1):08d}",
    }


MANUFACTURING = Domain(
    name="manufacturing",
    columns=["record_id", "production_id", "plant_id", "machine_id", "product_id", "shift",
             "operator_id", "production_date", "units_produced", "defective_units",
             "defect_rate", "downtime_minutes", "runtime_minutes", "temperature", "pressure",
             "vibration", "energy_usage", "maintenance_flag", "maintenance_date", "batch_id"],
    types={"record_id": "str", "production_id": "str", "plant_id": "str", "machine_id": "str",
           "product_id": "str", "shift": "str", "operator_id": "str", "production_date": "date",
           "units_produced": "int", "defective_units": "int", "defect_rate": "float",
           "downtime_minutes": "float", "runtime_minutes": "float", "temperature": "float",
           "pressure": "float", "vibration": "float", "energy_usage": "float",
           "maintenance_flag": "bool", "maintenance_date": "date", "batch_id": "str"},
    gen=gen_manufacturing, business_key="production_id", date="production_date",
    window=("2023-01-01", "2024-12-31"),
    measures={"units_produced": "sum", "defective_units": "sum", "downtime_minutes": "sum",
              "runtime_minutes": "sum", "energy_usage": "sum", "temperature": "mean",
              "vibration": "mean", "defect_rate": "none"},
    dimensions=["plant_id", "shift", "product_id", "maintenance_flag", "machine_id",
                "operator_id"],
    roles=dict(dims_low=["plant_id", "shift"], dim_mid="product_id", dim_hi="machine_id",
               binary="maintenance_flag", m_sum="defective_units", m_sum2="units_produced",
               m_mean="vibration", m_x="runtime_minutes", m_y="energy_usage",
               m_indep="temperature", entity="machine_id", m_none="defect_rate"),
    nullable={"temperature": 0.01, "production_date": 0.005, "downtime_minutes": 0.003},
    outlier_col="downtime_minutes",
    required=("trend", "changepoint", "group_compare", "outlier_detection", "correlation",
              "distribution", "concentration"),
    edge=("repeat_behaviour", "cohort_retention"),
    not_applicable=[
        {"analysis": "trend", "params": {"measure": "defect_rate", "grain": "month"},
         "expect": "reject", "why": "a rate is non-additive"},
    ],
)

# --------------------------------------------------------------------------- education


def gen_education(rng, i, ctx):
    d = rdate(rng, dt.date(2021, 1, 1), 1461)
    att = round(min(1.0, max(0.2, rng.gauss(0.85, 0.12))), 3)
    hours = round(max(0.0, rng.gauss(8 + 10 * (att - 0.85), 4)), 1)
    prev = round(min(4.0, max(0.0, rng.gauss(3.0, 0.5))), 2)
    assign = round(min(100, max(0, rng.gauss(60 + 30 * att, 10))), 1)
    mid = round(min(100, max(0, rng.gauss(55 + 25 * att + 2 * hours / 10, 12))), 1)
    final = round(min(100, max(0, rng.gauss(0.5 * mid + 35, 10))), 1)
    total = round(0.3 * assign + 0.3 * mid + 0.4 * final, 2)
    return {
        "enrollment_id": f"EN{i:09d}", "student_id": f"ST{rng.randrange(ctx['customers']):07d}",
        "course_id": f"CRS{rng.randrange(ctx['mid']):05d}",
        "instructor_id": f"IN{rng.randrange(ctx['small']):05d}",
        "department": pick(rng, ["math", "physics", "cs", "biology", "history", "arts",
                                 "economics", "languages"]),
        "enrollment_date": d, "semester": f"{d.year}-{'Spring' if d.month < 7 else 'Fall'}",
        "attendance_rate": att, "assignment_score": assign, "midterm_score": mid,
        "final_score": final, "total_score": total, "study_hours": hours,
        "previous_gpa": prev, "current_gpa": round(min(4.0, max(0.0, prev + (total - 70) / 60)), 2),
        "failed_subjects": 0 if rng.random() < 0.7 else rng.randint(1, 4),
        "dropout_flag": rng.random() < (0.02 + 0.2 * (1 - att)),
        "scholarship_flag": rng.random() < 0.18,
        "campus": pick(rng, ["main", "north", "online", "downtown"], [50, 20, 25, 5]),
    }


EDUCATION = Domain(
    name="education",
    columns=["record_id", "enrollment_id", "student_id", "course_id", "instructor_id",
             "department", "enrollment_date", "semester", "attendance_rate", "assignment_score",
             "midterm_score", "final_score", "total_score", "study_hours", "previous_gpa",
             "current_gpa", "failed_subjects", "dropout_flag", "scholarship_flag", "campus"],
    types={"record_id": "str", "enrollment_id": "str", "student_id": "str", "course_id": "str",
           "instructor_id": "str", "department": "str", "enrollment_date": "date",
           "semester": "str", "attendance_rate": "float", "assignment_score": "float",
           "midterm_score": "float", "final_score": "float", "total_score": "float",
           "study_hours": "float", "previous_gpa": "float", "current_gpa": "float",
           "failed_subjects": "int", "dropout_flag": "bool", "scholarship_flag": "bool",
           "campus": "str"},
    gen=gen_education, business_key="enrollment_id", date="enrollment_date",
    window=("2021-01-01", "2024-12-31"),
    measures={"study_hours": "sum", "failed_subjects": "sum", "total_score": "mean",
              "final_score": "mean", "attendance_rate": "mean", "current_gpa": "mean",
              "previous_gpa": "mean", "midterm_score": "mean"},
    dimensions=["department", "campus", "semester", "dropout_flag", "scholarship_flag",
                "course_id", "instructor_id", "student_id"],
    roles=dict(dims_low=["department", "campus"], dim_mid="semester", dim_hi="course_id",
               binary="scholarship_flag", m_sum="study_hours", m_sum2="failed_subjects",
               m_mean="total_score", m_x="midterm_score", m_y="final_score",
               m_indep="previous_gpa", entity="student_id"),
    nullable={"final_score": 0.02, "enrollment_date": 0.005, "campus": 0.003},
    outlier_col="study_hours",
    required=("group_compare", "hypothesis_test", "confidence_interval", "correlation",
              "distribution", "top_n", "cohort_retention"),
    not_applicable=[
        {"analysis": "correlation", "params": {"measure": "total_score", "against": "campus"},
         "expect": "reject", "why": "campus is text"},
    ],
)

DOMAINS = {d.name: d for d in (FINANCIAL, HR, SALES, MARKETING, CRM, ECOMMERCE, LOGISTICS,
                               HEALTHCARE, MANUFACTURING, EDUCATION)}


def contract_kwargs(domain, key: str) -> dict:
    return dict(grain=f"one row = one {domain.name} record", primary_key=[key],
                date_column=domain.date, measures=list(domain.measures),
                dimensions=list(domain.dimensions), aggregations=dict(domain.measures),
                measure_definitions={m: f"{m} as recorded" for m in domain.measures},
                analysis_window_start=domain.window[0], analysis_window_end=domain.window[1],
                known_exclusions=[{"rule": e["rule"], "reason": e["reason"]}
                                  for e in domain.exclusions] or None)


# --------------------------------------------------------------------------- the writer


def _render(v, kind: str) -> str:
    if v is None:
        return ""
    if kind == "bool":
        return "true" if v else "false"
    if kind == "ts":
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if kind == "date":
        return v.isoformat()
    if kind == "float":
        return repr(float(v)) if abs(v) >= 1e15 else f"{v:.6g}" if abs(v) < 1e-3 and v else str(v)
    return str(v)


def seed_for(domain: str, n: int, base: int) -> int:
    return base + sum(ord(c) * (k + 1) for k, c in enumerate(domain)) * 7919 + n


def write_dataset(domain: Domain, n: int, path: Path, base_seed: int, on_row=None) -> dict:
    """Write `n` generated rows (plus the inserted duplicates) and return the manifest.

    Anomaly rates: 0.3% of rows are emitted twice (exact duplicates, adjacent); 0.2% reuse an
    earlier row's business key; 0.05% of rows multiply the outlier column by 50; three rows
    (at fixed positions, when n allows) set it to 1e9; per-column null rates as declared.
    `on_row(row)` is called once per DISTINCT row, for streaming ground truth.
    """
    seed = seed_for(domain.name, n, base_seed)
    rng = random.Random(seed)
    ctx = domain.pools(n)
    counts = {"rows_generated": 0, "rows_written": 0, "duplicate_rows": 0,
              "duplicated_business_keys": 0, "outliers_x50": 0, "extreme_1e9": 0,
              "nulls": {c: 0 for c in domain.nullable}}
    extreme_at = {n // 3, n // 2, (2 * n) // 3} if n >= 1000 else set()
    recent: list[str] = []
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(domain.columns)
        for i in range(n):
            row = domain.gen(rng, i, ctx)
            row["record_id"] = f"{domain.name[:3].upper()}{i:09d}"
            if recent and rng.random() < 0.002:
                row[domain.business_key] = recent[rng.randrange(len(recent))]
                counts["duplicated_business_keys"] += 1
            else:
                recent.append(row[domain.business_key])
                if len(recent) > 1000:
                    recent.pop(0)
            for col, rate in domain.nullable.items():
                if row.get(col) is not None and rng.random() < rate:
                    row[col] = None
                    counts["nulls"][col] += 1
            oc = domain.outlier_col
            if oc and row.get(oc) is not None:
                if i in extreme_at:
                    row[oc] = 1e9
                    counts["extreme_1e9"] += 1
                elif rng.random() < 0.0005:
                    row[oc] = round(row[oc] * 50, 2)
                    counts["outliers_x50"] += 1
            cells = [_render(row.get(c), domain.types[c]) for c in domain.columns]
            w.writerow(cells)
            counts["rows_generated"] += 1
            counts["rows_written"] += 1
            if on_row:
                on_row(row)
            if rng.random() < 0.003:
                w.writerow(cells)
                counts["duplicate_rows"] += 1
                counts["rows_written"] += 1
    return {"domain": domain.name, "rows_requested": n, "seed": seed, "base_seed": base_seed,
            "path": str(path), "bytes": path.stat().st_size, "columns": domain.columns,
            "types": domain.types, "anomalies": counts, "pools": ctx,
            "window": list(domain.window), "date_column": domain.date,
            "business_key": domain.business_key,
            "anomaly_design": {
                "duplicate_row_rate": 0.003, "duplicated_business_key_rate": 0.002,
                "outlier_x50_rate": 0.0005, "extreme_positions": sorted(extreme_at),
                "null_rates": domain.nullable, "outside_window_rate": 0.01,
                "feb29_rate": 0.001, "window_edge_rate": 0.001,
                "constant_or_near_constant": "see generator (e.g. source_system, currency, country)",
                "rare_categories": "weights of 0.1% or less in pick() calls"}}
