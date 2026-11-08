"""Foreguild revenue model (v1.1). Every input below is a planning assumption, not field data,
except where a comment cites a source.

Run: python model.py            (month-by-month tables, lane break-even and lifetime values)
     python model.py --json     (machine-readable output)

Definitions
  RNR  Retained Net Revenue: buyer payments that survived the refund window,
       minus payouts to outside (subcontracted) agents. This is the futarchy metric.
  Contribution = RNR - variable costs (compute, verification, selling, client acquisition).
  Net = Contribution - fixed costs - decision-market budget.

Changes from v1.0
  - Compute per house task lowered (published cost per task for coding agents is about $1-3;
    assumption keeps a margin for failed attempts, CI and tooling).
  - Retainer and standing-order clients now churn, and every new client costs money to win.
  - Adds lane break-even and 5-year capped customer lifetime value.
"""
import json
import sys

CASES = {
    "low": dict(
        a_tasks=90, a_ticket=100, house_share=0.60, sub_payout=0.75, compute=12, verify=3, scope=10, refund=0.05,
        b_clients=3, c_orders=8, d_rnr=500,
    ),
    "base": dict(
        a_tasks=300, a_ticket=120, house_share=0.70, sub_payout=0.75, compute=8, verify=3, scope=8, refund=0.03,
        b_clients=8, c_orders=25, d_rnr=2000,
    ),
    "high": dict(
        a_tasks=900, a_ticket=140, house_share=0.75, sub_payout=0.75, compute=5, verify=3, scope=6, refund=0.02,
        b_clients=15, c_orders=60, d_rnr=6000,
    ),
}

B_PRICE, B_COST = 1500, 0.30   # market-ops retainer per client per month; delivery cost share
C_PRICE, C_COST = 600, 0.35    # standing order per buyer per month; delivery cost share
D_COST = 0.40                  # agent-to-agent API: compute + verification share
B_CHURN = 0.016                # per month; about 18% a year, the retainer-agency figure in one agency source (unverified sample)
C_CHURN = 0.03                 # per month; assumption, between retainer and project churn
B_CAC, C_CAC = 2500, 300       # cost to win one new client (assumption: sales and scoping time)
A_TASKS_PER_BUYER = 2.5        # assumption: tasks a code buyer orders before leaving
A_TO_C = 0.20                  # assumption: share of code buyers who convert to a standing order
START = dict(a=1, b=2, c=3, d=4)  # month each lane opens
SETUP = 20000                  # month 0: entity, escrow contract review, tooling
RAMP_EXP = 1.6                 # volume(m) = target * ((m - start + 1) / (13 - start)) ** RAMP_EXP


def fixed_cost(m):
    return 6500 if m <= 3 else 9500  # 2 part-time stewards, infra/CI, accounting, security review


def ramp(target, m, start):
    if m < start:
        return 0.0
    return target * ((m - start + 1) / (13 - start)) ** RAMP_EXP


def adds(target, m, start, churn):
    """New clients needed in month m: net growth plus replacement of churned clients."""
    prev = ramp(target, m - 1, start)
    return max(0.0, ramp(target, m, start) - prev) + churn * prev


def run(p):
    rows, cash, low_cash, breakeven = [], -SETUP, -SETUP, None
    for m in range(1, 13):
        tasks = ramp(p["a_tasks"], m, START["a"])
        gmv_a = tasks * p["a_ticket"]
        house = tasks * p["house_share"]
        sub = tasks - house
        rnr_a = gmv_a * (1 - p["refund"]) - sub * p["a_ticket"] * p["sub_payout"]
        var_a = house * p["compute"] + tasks * (p["verify"] + p["scope"])

        rnr_b = ramp(p["b_clients"], m, START["b"]) * B_PRICE
        rnr_c = ramp(p["c_orders"], m, START["c"]) * C_PRICE
        rnr_d = ramp(p["d_rnr"], m, START["d"])
        acq = adds(p["b_clients"], m, START["b"], B_CHURN) * B_CAC + adds(p["c_orders"], m, START["c"], C_CHURN) * C_CAC
        var_bcd = rnr_b * B_COST + rnr_c * C_COST + rnr_d * D_COST + acq

        rnr = rnr_a + rnr_b + rnr_c + rnr_d
        contribution = rnr - var_a - var_bcd
        market_budget = max(1000, 0.05 * rnr) if m >= 2 else 0
        net = contribution - fixed_cost(m) - market_budget
        cash += net
        low_cash = min(low_cash, cash)
        if breakeven is None and net >= 0:
            breakeven = m
        rows.append(dict(
            month=m, tasks=round(tasks), gmv_a=round(gmv_a), rnr_a=round(rnr_a), rnr_b=round(rnr_b),
            rnr_c=round(rnr_c), rnr_d=round(rnr_d), rnr=round(rnr), contribution=round(contribution),
            fixed=fixed_cost(m), market_budget=round(market_budget), net=round(net), cash=round(cash),
        ))
    return dict(
        rows=rows, breakeven_month=breakeven, peak_funding_need=round(-low_cash),
        year1_rnr=sum(r["rnr"] for r in rows), m12_rnr=rows[-1]["rnr"], m12_net=rows[-1]["net"],
        m12_market_budget=rows[-1]["market_budget"],
    )


def per_task(p):
    """Contribution per code task, house vs routed, and blended."""
    ticket = p["a_ticket"] * (1 - p["refund"])
    house = ticket - p["compute"] - p["verify"] - p["scope"]
    routed = ticket - p["a_ticket"] * p["sub_payout"] - p["verify"] - p["scope"]
    return house, routed, p["house_share"] * house + (1 - p["house_share"]) * routed


def lane_economics(p):
    """Break-even customers per lane (fixed costs of $9,500 + $1,000 market budget), payback and 5-year LTV."""
    fixed = 9500 + 1000
    _, _, blended = per_task(p)
    b_margin, c_margin = B_PRICE * (1 - B_COST), C_PRICE * (1 - C_COST)
    months = lambda c: (1 - (1 - c) ** 60) / c   # expected paying months within 5 years
    b_ltv, c_ltv = b_margin * months(B_CHURN), c_margin * months(C_CHURN)
    a_ltv = A_TASKS_PER_BUYER * blended + A_TO_C * c_ltv
    return dict(
        blended_task_contribution=round(blended, 2),
        a_breakeven_tasks=round(fixed / blended),
        b_margin=round(b_margin), b_breakeven_clients=round(fixed / b_margin, 1), b_payback_months=round(B_CAC / b_margin, 1),
        b_expected_months=round(months(B_CHURN), 1), b_ltv5=round(b_ltv),
        c_margin=round(c_margin), c_breakeven_orders=round(fixed / c_margin, 1), c_payback_months=round(C_CAC / c_margin, 1),
        c_expected_months=round(months(C_CHURN), 1), c_ltv5=round(c_ltv),
        a_ltv_one_off=round(A_TASKS_PER_BUYER * blended), a_ltv_with_conversion=round(a_ltv),
    )


if __name__ == "__main__":
    out = {k: dict(run(v), unit=dict(zip(("house", "routed", "blended"), [round(x, 2) for x in per_task(v)])),
                   lanes=lane_economics(v)) for k, v in CASES.items()}
    if "--json" in sys.argv:
        print(json.dumps(out, indent=1))
        sys.exit()
    for k, r in out.items():
        print(f"\n== {k}: break-even month {r['breakeven_month']}, peak funding need ${r['peak_funding_need']:,}, "
              f"year-1 RNR ${r['year1_rnr']:,}, M12 RNR ${r['m12_rnr']:,}, M12 net ${r['m12_net']:,}")
        print("   per code task (house / routed / blended):", r["unit"])
        print("   lanes:", r["lanes"])
        print(f"{'m':>3}{'tasks':>7}{'RNR_A':>9}{'RNR_B':>8}{'RNR_C':>8}{'RNR_D':>7}{'RNR':>9}{'contrib':>9}{'net':>9}{'cash':>10}")
        for row in r["rows"]:
            print(f"{row['month']:>3}{row['tasks']:>7}{row['rnr_a']:>9,}{row['rnr_b']:>8,}{row['rnr_c']:>8,}"
                  f"{row['rnr_d']:>7,}{row['rnr']:>9,}{row['contribution']:>9,}{row['net']:>9,}{row['cash']:>10,}")
