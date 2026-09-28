"""The LLM benchmark's question bank: 120 questions, 12 per domain (Step 13, handoff Part B4-B8).

Each question carries its gold annotation, fixed before any model is run:
  intent        one of INTENTS
  metrics       the measures a correct plan may use (any one of them is right)
  dims          the dimensions a correct plan may use ([] when none is needed)
  time          whether a time range or period comparison is part of the question
  ok            analyses a correct plan may use; the first step should be one of them
  bad           analyses that answer a different question or cannot run here
  flag          None, or INSUFFICIENT_DATA / RESEARCH_REQUIRED / REJECT for the adversarial ones
  calls         (min, max) analytical calls a sufficient end-to-end answer needs
  marketing     part of the marketing subset (B15)
  e2e           one of the 40 end-to-end questions (4 per domain)
Several valid paths exist for the advanced ones: `ok` lists every analysis that belongs to one.
"""

from __future__ import annotations

INTENTS = ("ranking", "trend", "comparison", "change_explanation", "driver", "relationship",
           "distribution", "anomaly", "retention", "significance", "concentration", "refuse")

_Q: list[dict] = []


def q(domain, level, text, intent, metrics, dims, ok, *, time=False, flag=None, bad=(),
      calls=(1, 3), mkt=False, e2e=False):
    _Q.append({"domain": domain, "level": level, "text": text, "intent": intent,
               "metrics": list(metrics), "dims": list(dims), "time": time, "ok": list(ok),
               "bad": list(bad), "flag": flag, "calls": list(calls), "marketing": mkt,
               "e2e": e2e})


CHANGE = ["period_compare", "growth_decomposition", "mix_shift", "trend", "changepoint",
          "ranking_shift", "top_n", "group_compare", "driver_analysis", "seasonality"]
SIG = ["hypothesis_test", "effect_size", "confidence_interval", "sample_adequacy",
       "group_compare"]

# --------------------------------------------------------------------------- financial
D = "financial"
q(D, "simple", "Which five branches brought in the most revenue?", "ranking", ["revenue"],
  ["branch"], ["top_n", "pareto", "group_compare"], e2e=True)
q(D, "simple", "Show monthly profit over the whole period.", "trend", ["profit"], [],
  ["trend"], time=True)
q(D, "simple", "How is transaction amount distributed?", "distribution", ["amount"], [],
  ["distribution", "summary_stats"])
q(D, "intermediate", "Did revenue in December 2024 differ from November 2024, and which "
  "payment methods explain the change?", "change_explanation", ["revenue"], ["payment_method"],
  CHANGE, time=True, calls=(2, 4), e2e=True)
q(D, "intermediate", "Are a few customers responsible for most of the revenue?", "concentration",
  ["revenue"], ["customer_id"], ["pareto", "concentration", "top_n"])
q(D, "intermediate", "Is the risk score different for fraudulent and non-fraudulent "
  "transactions, beyond noise?", "significance", ["risk_score"], ["fraud_flag"], SIG,
  calls=(1, 3))
q(D, "intermediate", "Which expense values look unusual?", "anomaly", ["expense"], [],
  ["outlier_detection", "distribution"])
q(D, "advanced", "Profit fell in late 2024. Find when it started, which categories drove it, "
  "and whether the shift is structural or seasonal.", "change_explanation", ["profit"],
  ["category", "branch", "transaction_type"], CHANGE, time=True, calls=(3, 7), e2e=True)
q(D, "advanced", "Budget and actual spend: do they move together, and does any branch break "
  "the pattern?", "relationship", ["budget", "actual"], ["branch"],
  ["correlation", "bivariate", "correlated_shift", "group_compare", "top_n"], calls=(2, 5))
q(D, "advanced", "What explains most of the variation in revenue across our dimensions?",
  "driver", ["revenue"], ["category", "branch", "payment_method", "transaction_type"],
  ["driver_analysis", "group_compare", "top_n"], calls=(1, 4))
q(D, "adversarial", "Sum the customer IDs by branch.", "refuse", [], ["branch"], [],
  flag="REJECT", bad=["top_n", "summary_stats"], calls=(0, 1), e2e=True)
q(D, "adversarial", "Prove that the new payment method caused the fraud increase.", "refuse",
  ["risk_score"], ["payment_method", "fraud_flag"], SIG + ["trend"], flag="INSUFFICIENT_DATA",
  calls=(0, 3))

# --------------------------------------------------------------------------- hr
D = "hr"
q(D, "simple", "Which departments have the highest attrition?", "ranking", ["salary"],
  ["department", "attrition_flag"], ["cross_tab", "frequency", "group_compare", "top_n"],
  e2e=True)
q(D, "simple", "What is the average performance score by location?", "comparison",
  ["performance_score"], ["location"], ["group_compare", "top_n"])
q(D, "simple", "How many hires did we make each year?", "trend", [], [],
  ["calendar_coverage", "trend", "frequency"], time=True)
q(D, "intermediate", "Do employees who leave have lower performance scores than those who "
  "stay?", "significance", ["performance_score"], ["attrition_flag"], SIG, calls=(1, 3),
  e2e=True)
q(D, "intermediate", "Is training time related to performance?", "relationship",
  ["training_hours", "performance_score"], [], ["correlation", "bivariate"])
q(D, "intermediate", "Which teams concentrate most of the bonus spend?", "concentration",
  ["bonus"], ["team"], ["pareto", "concentration", "top_n"])
q(D, "intermediate", "Which salaries are outliers?", "anomaly", ["salary"], [],
  ["outlier_detection", "distribution"])
q(D, "advanced", "Attrition seems higher in some places. Identify where, whether the "
  "difference is larger than chance, and how big it is.", "significance", ["salary",
                                                                            "performance_score"],
  ["location", "department", "attrition_flag"], ["cross_tab", "hypothesis_test", "effect_size",
                                                  "group_compare", "frequency",
                                                  "sample_adequacy"], calls=(2, 5), e2e=True)
q(D, "advanced", "Which dimension explains most of the difference in salary?", "driver",
  ["salary"], ["department", "job_role", "location", "team"], ["driver_analysis",
                                                                "group_compare"], calls=(1, 3))
q(D, "advanced", "Have hiring volumes shifted over time and is there a point where they "
  "changed level?", "change_explanation", [], [], ["calendar_coverage", "trend", "changepoint",
                                                   "seasonality"], time=True, calls=(2, 4))
q(D, "adversarial", "Correlate employee name with salary.", "refuse", ["salary"], [], [],
  flag="REJECT", bad=["correlation"], calls=(0, 1), e2e=True)
q(D, "adversarial", "We compared two employees. Confirm that the difference in their "
  "performance is statistically significant.", "refuse", ["performance_score"], [],
  ["sample_adequacy"], flag="INSUFFICIENT_DATA", calls=(0, 2))

# --------------------------------------------------------------------------- sales
D = "sales"
q(D, "simple", "Which products categories have the highest profit?", "ranking", ["profit"],
  ["product_category"], ["top_n", "group_compare", "pareto"], e2e=True)
q(D, "simple", "Show monthly net sales.", "trend", ["net_sales"], [], ["trend"], time=True)
q(D, "simple", "What share of orders are returned?", "distribution", [], ["returned_flag"],
  ["frequency", "confidence_interval"])
q(D, "intermediate", "Which regions are losing net sales compared with the previous quarter, "
  "and which channels explain the decline?", "change_explanation", ["net_sales"],
  ["region", "channel"], CHANGE, time=True, calls=(2, 5), e2e=True)
q(D, "intermediate", "Is discount related to net sales per order?", "relationship",
  ["discount", "net_sales"], [], ["correlation", "bivariate"])
q(D, "intermediate", "Do our top cities hold most of the revenue?", "concentration",
  ["net_sales"], ["city"], ["pareto", "concentration", "top_n"])
q(D, "intermediate", "Are returns more common in some channels than others beyond chance?",
  "significance", [], ["channel", "returned_flag"], ["hypothesis_test", "cross_tab",
                                                     "effect_size"], calls=(1, 3))
q(D, "advanced", "Net sales fell sharply in the last quarter. Determine whether it is lower "
  "volume, product mix, geography, seasonality or a structural change.", "change_explanation",
  ["net_sales", "quantity"], ["product_category", "region", "channel"], CHANGE, time=True,
  calls=(3, 8), e2e=True)
q(D, "advanced", "Which salespeople improved their ranking between 2023 and 2024?",
  "ranking", ["net_sales"], ["salesperson_id"], ["ranking_shift", "top_n"], time=True,
  calls=(1, 3))
q(D, "advanced", "How often do customers come back, and do some cohorts retain better?",
  "retention", [], ["customer_id"], ["repeat_behaviour", "cohort_retention"], calls=(2, 3))
q(D, "adversarial", "Sum the order IDs by region.", "refuse", [], ["region"], [],
  flag="REJECT", bad=["top_n", "summary_stats"], calls=(0, 1), e2e=True)
q(D, "adversarial", "Did Competitor X's campaign cause our May sales decline?", "refuse",
  ["net_sales"], [], ["period_compare", "trend"], flag="RESEARCH_REQUIRED", calls=(0, 3))

# --------------------------------------------------------------------------- marketing
D = "marketing"
q(D, "simple", "Which five campaigns generated the most revenue?", "ranking", ["revenue"],
  ["campaign_id"], ["top_n", "pareto"], mkt=True, e2e=True)
q(D, "simple", "Show monthly spend by channel.", "trend", ["spend"], ["channel"],
  ["trend", "cross_tab", "group_compare"], time=True, mkt=True)
q(D, "simple", "Which audience segments bring the most conversions?", "ranking",
  ["conversions"], ["audience_segment"], ["top_n", "group_compare", "pareto"], mkt=True)
q(D, "intermediate", "Which marketing channels improved conversions compared with the previous "
  "quarter?", "change_explanation", ["conversions"], ["channel"],
  ["growth_decomposition", "period_compare", "ranking_shift", "mix_shift", "trend"],
  time=True, calls=(1, 4), mkt=True, e2e=True)
q(D, "intermediate", "Which channels increased spend without a proportional revenue increase?",
  "change_explanation", ["spend", "revenue"], ["channel"],
  ["growth_decomposition", "period_compare", "group_compare", "trend", "correlation"],
  time=True, calls=(2, 5), mkt=True)
q(D, "intermediate", "Is spend related to clicks across campaigns?", "relationship",
  ["spend", "clicks"], [], ["correlation", "bivariate"], mkt=True)
q(D, "intermediate", "Does retargeting change the click-through rate beyond noise?",
  "significance", ["ctr"], ["retargeting_flag"], SIG, calls=(1, 3), mkt=True)
q(D, "advanced", "Revenue per unit of spend fell even though conversions rose. What changed "
  "in channel mix, spend and revenue contribution?", "change_explanation",
  ["revenue", "spend", "conversions"], ["channel", "campaign_type", "audience_segment"],
  CHANGE, time=True, calls=(3, 8), mkt=True, e2e=True)
q(D, "advanced", "Which campaign's revenue shows a structural change rather than normal "
  "seasonality?", "change_explanation", ["revenue"], ["campaign_id"],
  ["changepoint", "seasonality", "trend", "top_n"], time=True, calls=(2, 5), mkt=True)
q(D, "advanced", "Which geography and device combination carries the most leads, and is "
  "that share concentrated?", "concentration", ["leads"], ["geography", "device"],
  ["cross_tab", "pareto", "concentration", "top_n"], calls=(2, 4), mkt=True)
q(D, "adversarial", "Prove that Campaign X caused customer churn.", "refuse", ["revenue"],
  ["campaign_id"], [], flag="INSUFFICIENT_DATA", calls=(0, 2), mkt=True, e2e=True)
q(D, "adversarial", "Sum ROAS across all campaigns to get total ROAS.", "refuse", ["roas"], [],
  ["summary_stats"], flag="REJECT", bad=["top_n"], calls=(0, 2), mkt=True)

# --------------------------------------------------------------------------- crm
D = "crm"
q(D, "simple", "Which customer segments spend the most?", "ranking", ["total_spend"],
  ["segment"], ["top_n", "group_compare", "pareto"], e2e=True)
q(D, "simple", "How is satisfaction score distributed?", "distribution",
  ["satisfaction_score"], [], ["distribution", "summary_stats"])
q(D, "simple", "Which acquisition channels bring the most customers?", "ranking", [],
  ["acquisition_channel"], ["frequency", "top_n"], mkt=True)
q(D, "intermediate", "Do churned customers have lower satisfaction than retained ones?",
  "significance", ["satisfaction_score"], ["churn_flag"], SIG, calls=(1, 3), e2e=True)
q(D, "intermediate", "Is the number of support tickets related to satisfaction?",
  "relationship", ["support_tickets", "satisfaction_score"], [], ["correlation", "bivariate"])
q(D, "intermediate", "Which regions have unusually many complaints?", "anomaly",
  ["complaints"], ["region"], ["group_compare", "top_n", "outlier_detection"])
q(D, "intermediate", "Did signups by acquisition channel change between 2023 and 2024?",
  "change_explanation", [], ["acquisition_channel"],
  ["ranking_shift", "cross_tab", "period_compare", "trend", "frequency"], time=True,
  calls=(1, 4), mkt=True)
q(D, "advanced", "Customer retention fell. Identify when the decline started, which cohorts "
  "contributed most, and whether the effect is statistically meaningful.", "retention",
  ["total_spend"], ["segment", "churn_flag"],
  ["cohort_retention", "repeat_behaviour", "changepoint", "trend", "hypothesis_test",
   "effect_size", "calendar_coverage"], time=True, calls=(3, 7), e2e=True)
q(D, "advanced", "Which dimension explains most of the variation in total spend?", "driver",
  ["total_spend"], ["segment", "region", "acquisition_channel", "loyalty_status"],
  ["driver_analysis", "group_compare"], calls=(1, 3))
q(D, "advanced", "Are a few customers carrying most of the spend, and are they concentrated "
  "in one loyalty status?", "concentration", ["total_spend"], ["customer_id", "loyalty_status"],
  ["pareto", "concentration", "top_n", "group_compare"], calls=(2, 4))
q(D, "adversarial", "Correlate customer name with total spend.", "refuse", ["total_spend"],
  [], [], flag="REJECT", bad=["correlation"], calls=(0, 1), e2e=True)
q(D, "adversarial", "Did the competitor's loyalty programme cause our churn increase?",
  "refuse", [], ["churn_flag"], ["trend", "frequency"], flag="RESEARCH_REQUIRED",
  calls=(0, 3))

# --------------------------------------------------------------------------- ecommerce
D = "ecommerce"
q(D, "simple", "Which categories bring the highest payment value?", "ranking",
  ["payment_value"], ["category"], ["top_n", "group_compare", "pareto"], e2e=True)
q(D, "simple", "Show monthly order payment value.", "trend", ["payment_value"], [], ["trend"],
  time=True)
q(D, "simple", "Which traffic sources bring the most orders?", "ranking", [],
  ["traffic_source"], ["frequency", "top_n"], mkt=True)
q(D, "intermediate", "Did payment value change between November and December 2024, and "
  "which traffic sources explain it?", "change_explanation", ["payment_value"],
  ["traffic_source"], CHANGE, time=True, calls=(2, 4), mkt=True, e2e=True)
q(D, "intermediate", "Do returned orders have lower review scores beyond chance?",
  "significance", ["review_score"], ["returned_flag"], SIG, calls=(1, 3))
q(D, "intermediate", "Is quantity related to payment value?", "relationship",
  ["quantity", "payment_value"], [], ["correlation", "bivariate"])
q(D, "intermediate", "How often do customers buy again?", "retention", [], ["customer_id"],
  ["repeat_behaviour", "cohort_retention"], calls=(1, 2), mkt=True)
q(D, "advanced", "Payment value dropped last quarter. Is it fewer orders, a shift in category "
  "mix, or a change in how much each order is worth?", "change_explanation",
  ["payment_value", "quantity"], ["category", "traffic_source", "device"], CHANGE, time=True,
  calls=(3, 7), e2e=True)
q(D, "advanced", "Which sellers moved up or down the ranking between 2023 and 2024?",
  "ranking", ["payment_value"], ["seller_id"], ["ranking_shift", "top_n"], time=True,
  calls=(1, 3))
q(D, "advanced", "What explains most of the variation in review score?", "driver",
  ["review_score"], ["category", "payment_type", "device", "traffic_source"],
  ["driver_analysis", "group_compare"], calls=(1, 3))
q(D, "adversarial", "Sum the product IDs by category.", "refuse", [], ["category"], [],
  flag="REJECT", bad=["top_n", "summary_stats"], calls=(0, 1), e2e=True)
q(D, "adversarial", "We have two orders from the new device type. Confirm it converts better.",
  "refuse", ["payment_value"], ["device"], ["sample_adequacy"], flag="INSUFFICIENT_DATA",
  calls=(0, 2))

# --------------------------------------------------------------------------- logistics
D = "logistics"
q(D, "simple", "Show monthly delivery success.", "trend", [], ["status"],
  ["trend", "cross_tab", "frequency"], time=True, e2e=True)
q(D, "simple", "Which hubs handle the most cash on delivery?", "ranking", ["cod_amount"],
  ["hub_id"], ["top_n", "pareto"])
q(D, "simple", "How are delivery times distributed?", "distribution", ["delivery_time_hours"],
  [], ["distribution", "summary_stats"])
q(D, "intermediate", "Which logistics hubs are breaching SLA more often than before?",
  "change_explanation", [], ["hub_id", "sla_breach"],
  ["cross_tab", "ranking_shift", "period_compare", "trend", "group_compare", "frequency"],
  time=True, calls=(2, 4), e2e=True)
q(D, "intermediate", "Is distance related to delivery time?", "relationship",
  ["distance_km", "delivery_time_hours"], [], ["correlation", "bivariate"])
q(D, "intermediate", "Do SLA breaches take longer to deliver beyond chance?", "significance",
  ["delivery_time_hours"], ["sla_breach"], SIG, calls=(1, 3))
q(D, "intermediate", "Which delivery times are anomalous?", "anomaly", ["delivery_time_hours"],
  [], ["outlier_detection", "distribution"])
q(D, "advanced", "Delivery times got worse. Find when, which zones drove it, and whether it "
  "is a level change or seasonal.", "change_explanation", ["delivery_time_hours"],
  ["zone", "hub_id"], CHANGE, time=True, calls=(3, 7), e2e=True)
q(D, "advanced", "Which riders have unusual failed-attempt patterns?", "anomaly", ["attempts"],
  ["rider_id", "fake_attempt_flag"], ["top_n", "group_compare", "outlier_detection",
                                      "cross_tab"], calls=(1, 4))
q(D, "advanced", "What explains most of the variation in delivery time?", "driver",
  ["delivery_time_hours"], ["zone", "hub_id", "status"], ["driver_analysis", "group_compare"],
  calls=(1, 3))
q(D, "adversarial", "Sum the rider IDs by zone.", "refuse", [], ["zone"], [], flag="REJECT",
  bad=["top_n", "summary_stats"], calls=(0, 1), e2e=True)
q(D, "adversarial", "Did the fuel price rise cause our delivery delays?", "refuse",
  ["delivery_time_hours"], [], ["trend", "changepoint"], flag="RESEARCH_REQUIRED",
  calls=(0, 3))

# --------------------------------------------------------------------------- healthcare
D = "healthcare"
q(D, "simple", "Which hospitals have the highest treatment cost?", "ranking",
  ["treatment_cost"], ["hospital_id"], ["top_n", "group_compare", "pareto"], e2e=True)
q(D, "simple", "Show monthly admissions.", "trend", [], [], ["calendar_coverage", "trend",
                                                              "frequency"], time=True)
q(D, "simple", "How is length of stay distributed?", "distribution", ["length_of_stay"], [],
  ["distribution", "summary_stats"])
q(D, "intermediate", "Does length of stay differ between treatment groups beyond chance, and "
  "by how much?", "significance", ["length_of_stay"], ["treatment_group"], SIG, calls=(2, 3),
  e2e=True)
q(D, "intermediate", "Is length of stay related to treatment cost?", "relationship",
  ["length_of_stay", "treatment_cost"], [], ["correlation", "bivariate"])
q(D, "intermediate", "Which diagnosis groups concentrate most of the cost?", "concentration",
  ["treatment_cost"], ["diagnosis_group"], ["pareto", "concentration", "top_n"])
q(D, "intermediate", "Which lab values are unusual?", "anomaly", ["lab_value"], [],
  ["outlier_detection", "distribution"])
q(D, "advanced", "Treatment cost rose this year. Is it more admissions, longer stays, or a "
  "shift in diagnosis mix?", "change_explanation", ["treatment_cost", "length_of_stay"],
  ["diagnosis_group", "hospital_id"], CHANGE, time=True, calls=(3, 7), e2e=True)
q(D, "advanced", "Which dimension explains most of the variation in length of stay?", "driver",
  ["length_of_stay"], ["diagnosis_group", "hospital_id", "treatment_group", "outcome"],
  ["driver_analysis", "group_compare"], calls=(1, 3))
q(D, "advanced", "How often do patients come back, and do some admission cohorts return more?",
  "retention", [], ["patient_id"], ["repeat_behaviour", "cohort_retention"], calls=(2, 3))
q(D, "adversarial", "Sum the patient IDs by hospital.", "refuse", [], ["hospital_id"], [],
  flag="REJECT", bad=["top_n", "summary_stats"], calls=(0, 1), e2e=True)
q(D, "adversarial", "Prove the new treatment caused lower mortality.", "refuse",
  ["length_of_stay"], ["treatment_group", "outcome"], SIG + ["cross_tab"],
  flag="INSUFFICIENT_DATA", calls=(0, 3))

# --------------------------------------------------------------------------- manufacturing
D = "manufacturing"
q(D, "simple", "Which plants produce the most defective units?", "ranking",
  ["defective_units"], ["plant_id"], ["top_n", "group_compare", "pareto"], e2e=True)
q(D, "simple", "Show monthly units produced.", "trend", ["units_produced"], [], ["trend"],
  time=True)
q(D, "simple", "How is vibration distributed?", "distribution", ["vibration"], [],
  ["distribution", "summary_stats"])
q(D, "intermediate", "Do machines under maintenance produce more defects beyond chance?",
  "significance", ["defective_units"], ["maintenance_flag"], SIG, calls=(1, 3), e2e=True)
q(D, "intermediate", "Is runtime related to energy usage?", "relationship",
  ["runtime_minutes", "energy_usage"], [], ["correlation", "bivariate"])
q(D, "intermediate", "Which machines concentrate most of the downtime?", "concentration",
  ["downtime_minutes"], ["machine_id"], ["pareto", "concentration", "top_n"])
q(D, "intermediate", "Which temperature readings are anomalous?", "anomaly", ["temperature"],
  [], ["outlier_detection", "distribution"])
q(D, "advanced", "Defects rose recently. Find when, which shifts and products drove it, and "
  "whether it is a level change.", "change_explanation", ["defective_units"],
  ["shift", "product_id", "plant_id"], CHANGE, time=True, calls=(3, 7), e2e=True)
q(D, "advanced", "Do vibration and defects change level at the same time?", "relationship",
  ["vibration", "defective_units"], [], ["correlated_shift", "changepoint", "correlation"],
  time=True, calls=(1, 3))
q(D, "advanced", "What explains most of the variation in downtime?", "driver",
  ["downtime_minutes"], ["plant_id", "shift", "product_id", "maintenance_flag"],
  ["driver_analysis", "group_compare"], calls=(1, 3))
q(D, "adversarial", "Sum the machine IDs by plant.", "refuse", [], ["plant_id"], [],
  flag="REJECT", bad=["top_n", "summary_stats"], calls=(0, 1), e2e=True)
q(D, "adversarial", "Did the supplier change cause the defect increase?", "refuse",
  ["defective_units"], [], ["trend", "changepoint"], flag="RESEARCH_REQUIRED", calls=(0, 3))

# --------------------------------------------------------------------------- education
D = "education"
q(D, "simple", "Which departments have the highest average total score?", "ranking",
  ["total_score"], ["department"], ["top_n", "group_compare"], e2e=True)
q(D, "simple", "Show enrolments per semester.", "trend", [], ["semester"],
  ["frequency", "calendar_coverage", "trend"], time=True)
q(D, "simple", "How are final scores distributed?", "distribution", ["final_score"], [],
  ["distribution", "summary_stats"])
q(D, "intermediate", "Do scholarship students score higher beyond chance, and by how much?",
  "significance", ["total_score", "final_score"], ["scholarship_flag"], SIG, calls=(2, 3),
  e2e=True)
q(D, "intermediate", "Is midterm score related to final score?", "relationship",
  ["midterm_score", "final_score"], [], ["correlation", "bivariate"])
q(D, "intermediate", "Which campuses have unusually many failed subjects?", "anomaly",
  ["failed_subjects"], ["campus"], ["group_compare", "top_n", "outlier_detection"])
q(D, "intermediate", "Did the course ranking by study hours change between 2023 and 2024?",
  "ranking", ["study_hours"], ["course_id"], ["ranking_shift", "top_n"], time=True,
  calls=(1, 3))
q(D, "advanced", "Dropout seems to be rising. Identify when, which departments contribute "
  "most, and whether the difference is meaningful.", "change_explanation", ["total_score"],
  ["department", "dropout_flag", "campus"],
  ["cross_tab", "trend", "changepoint", "hypothesis_test", "effect_size", "frequency",
   "group_compare", "calendar_coverage"], time=True, calls=(3, 7), e2e=True)
q(D, "advanced", "What explains most of the variation in final score?", "driver",
  ["final_score"], ["department", "campus", "semester", "scholarship_flag"],
  ["driver_analysis", "group_compare"], calls=(1, 3))
q(D, "advanced", "How many students come back for another course, and do some intake cohorts "
  "return more?", "retention", [], ["student_id"], ["repeat_behaviour", "cohort_retention"],
  calls=(2, 3))
q(D, "adversarial", "Correlate student name with GPA.", "refuse", ["current_gpa"], [], [],
  flag="REJECT", bad=["correlation"], calls=(0, 1), e2e=True)
q(D, "adversarial", "Did the new national curriculum cause the drop in scores?", "refuse",
  ["total_score"], [], ["trend", "changepoint"], flag="RESEARCH_REQUIRED", calls=(0, 3))


QUESTIONS = [{"id": f"{x['domain'][:3].upper()}{i % 12 + 1:02d}", **x} for i, x in
             enumerate(_Q)]

assert len(QUESTIONS) == 120, len(QUESTIONS)
assert len({x["id"] for x in QUESTIONS}) == 120
assert sum(x["e2e"] for x in QUESTIONS) == 40, sum(x["e2e"] for x in QUESTIONS)
assert sum(x["marketing"] for x in QUESTIONS) >= 15
assert all(x["intent"] in INTENTS for x in QUESTIONS)
for _d in {x["domain"] for x in QUESTIONS}:
    _lv = [x["level"] for x in QUESTIONS if x["domain"] == _d]
    assert (_lv.count("simple"), _lv.count("intermediate"), _lv.count("advanced"),
            _lv.count("adversarial")) == (3, 4, 3, 2), (_d, _lv)
