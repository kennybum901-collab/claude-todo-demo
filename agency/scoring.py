"""Lead scoring: estimate each lead's chance of becoming a client.

    p = niche_rate x city_lift x reputation_factor

  * niche_rate - your close rate for that niche, smoothed toward the overall
    rate (Bayesian average with PRIOR_WEIGHT pseudo-leads) so 2 lucky sales
    don't dominate. Before you have data, an industry-style default is used.
  * city_lift - the city's smoothed close rate relative to the overall rate.
  * reputation_factor - established businesses (good rating, many reviews)
    have customers and budget; brand-new or poorly rated ones rarely buy.

score = 0-100 (p mapped onto a saturating curve), stored on the lead with a
short human-readable reason. Used to order site building and outreach, and
MIN_LEAD_SCORE can skip weak leads entirely.
"""
from __future__ import annotations

from collections import defaultdict

from .config import Config
from .db import DB

PRIOR_WEIGHT = 20          # pseudo-leads pulling a segment toward the overall rate
DEFAULT_RATE = 0.03        # assumed close rate before you have any results
START_KINDS = ("setup+first_month", "winback")


def reputation_factor(rating: float | None, reviews: int | None) -> tuple[float, str]:
    if rating is None and not reviews:
        return 0.9, "no reviews data"
    reviews = reviews or 0
    f = 1.0
    if reviews >= 100:
        f *= 1.3
    elif reviews >= 25:
        f *= 1.15
    elif reviews < 5:
        f *= 0.6
    if rating is not None:
        if rating >= 4.5:
            f *= 1.15
        elif rating < 3.5:
            f *= 0.6
    label = f"{rating or '?'}★/{reviews} reviews"
    return f, label


def to_score(p: float) -> int:
    """Saturating map: 3% -> ~38, 6% -> ~55, 10% -> ~67, 20% -> ~80."""
    return max(0, min(100, round(100 * p / (p + 0.05))))


class LeadScorer:
    def __init__(self, db: DB, cfg: Config):
        self.db, self.cfg = db, cfg
        self._fit()

    def _fit(self) -> None:
        q = self.db.conn.execute
        emailed = {r[0] for r in q("SELECT DISTINCT lead_id FROM events WHERE bot='outreach' "
                                    "AND kind IN ('sent','dry_send') AND lead_id IS NOT NULL")}
        won = {r[0] for r in q("SELECT DISTINCT lead_id FROM payments WHERE kind IN (?,?)", START_KINDS)}
        emailed |= won
        self.niche = defaultdict(lambda: [0, 0])   # key -> [emailed, won]
        self.city = defaultdict(lambda: [0, 0])
        for r in q("SELECT id, niche, city FROM leads"):
            if r["id"] not in emailed:
                continue
            for table, key in ((self.niche, r["niche"]), (self.city, r["city"])):
                cell = table[(key or "").lower()]
                cell[0] += 1
                cell[1] += r["id"] in won
        total_e, total_w = len(emailed), len(won)
        # overall rate itself is smoothed toward the default until there's volume
        self.overall = (total_w + DEFAULT_RATE * PRIOR_WEIGHT * 5) / (total_e + PRIOR_WEIGHT * 5)

    def _smoothed(self, table, key) -> tuple[float, int]:
        e, w = table.get((key or "").lower(), (0, 0))
        return (w + self.overall * PRIOR_WEIGHT) / (e + PRIOR_WEIGHT), e

    def score(self, lead) -> tuple[int, float, str]:
        niche_rate, n_e = self._smoothed(self.niche, lead["niche"])
        city_rate, c_e = self._smoothed(self.city, lead["city"])
        city_lift = city_rate / self.overall if self.overall else 1.0
        rep, rep_label = reputation_factor(lead["rating"], lead["review_count"])
        p = niche_rate * city_lift * rep
        reasons = [f"{lead['niche'] or 'unknown niche'} {niche_rate:.1%}" + ("" if n_e >= PRIOR_WEIGHT else " (early)")]
        if abs(city_lift - 1) >= 0.1:
            reasons.append(f"{lead['city'] or 'city'} x{city_lift:.1f}")
        reasons.append(rep_label)
        return to_score(p), p, " · ".join(reasons)

    def score_all(self, statuses: tuple[str, ...] = ("new", "built", "deployed", "contacted")) -> int:
        n = 0
        marks = ",".join("?" for _ in statuses)
        rows = self.db.conn.execute(f"SELECT * FROM leads WHERE status IN ({marks})", statuses).fetchall()
        with self.db.tx() as c:
            for lead in rows:
                s, _, why = self.score(lead)
                if lead["score"] != s or lead["score_reason"] != why:
                    c.execute("UPDATE leads SET score = ?, score_reason = ? WHERE id = ?", (s, why, lead["id"]))
                    n += 1
        return n


def expected_value_cents(p: float, cfg: Config, months: int = 12) -> int:
    return int(p * (cfg.setup_fee_cents + months * cfg.monthly_fee_cents))

