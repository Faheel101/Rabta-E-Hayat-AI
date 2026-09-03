"""Daily forecast inference (spec §6).

Routes each series by demand regime, generates P10/P50/P90 over a 30-day
horizon, and honours the MODEL_FALLBACK verdict from the most recent backtest:
a series whose model loses to both baselines is served by the baseline and
labelled as such, rather than shipped quietly (spec §6.3).
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import insert, select

from config.settings import DEMO_DATE
from core import config
from db.models import Component, Facility, Forecast, ForecastMetric, new_id
from db.session import SessionLocal, init_db
from engines.forecast.features import (
    FEATURES,
    KEY_COLS,
    add_lag_features,
    build_panel,
    classify_regime,
    load_series,
    route_regime,
)
from engines.forecast.inference import (
    build_series_states,
    forecast_baselines,
    forecast_lgbm,
    forecast_statistical,
)
from engines.forecast.models import (
    LIGHTGBM_AVAILABLE,
    count_quantiles,
    dispersion,
    fit_calendar_factors,
    train_quantile_models,
)

SEED = config.SEED
HORIZON_DAYS = 30
REGIME_WINDOW_DAYS = 150

DEMO_DATETIME = datetime.combine(DEMO_DATE, time(8, 0, 0), tzinfo=timezone.utc)
HISTORY_END = DEMO_DATE - timedelta(days=1)
HISTORY_START = DEMO_DATE - timedelta(days=config.HISTORY_DAYS)


def bulk_insert(session, model, rows, chunk_size=5000):
    for start in range(0, len(rows), chunk_size):
        session.execute(insert(model), rows[start : start + chunk_size])
        session.flush()


def load_fallback_keys(session) -> set:
    """Series the latest backtest found were not beaten by their own baseline."""

    latest_run = session.scalar(
        select(ForecastMetric.run_id)
        .order_by(ForecastMetric.generated_at.desc())
        .limit(1)
    )

    if latest_run is None:
        return set()

    rows = session.execute(
        select(
            ForecastMetric.facility_id,
            ForecastMetric.component_id,
            ForecastMetric.blood_group_id,
        ).where(
            ForecastMetric.run_id == latest_run,
            ForecastMetric.horizon_days == 7,
            ForecastMetric.is_fallback.is_(True),
        )
    ).all()

    return {(row[0], row[1], row[2]) for row in rows}


def make_row(run_id, key, target_date, horizon_days, quantiles, model_version):
    p10, p50, p90 = quantiles

    return {
        "id": new_id(),
        "run_id": run_id,
        "facility_id": key[0],
        "component_id": key[1],
        "blood_group_id": key[2],
        "target_date": target_date,
        "horizon_days": horizon_days,
        "p10": float(p10),
        "p50": float(p50),
        "p90": float(p90),
        "model_version": model_version,
        "generated_at": DEMO_DATETIME,
    }


def main():
    init_db()
    session = SessionLocal()

    try:
        print(f"LightGBM available: {LIGHTGBM_AVAILABLE}")

        facility_df, component_df, group_df, agg_df = load_series(
            session, HISTORY_START, HISTORY_END
        )

        component_codes = dict(
            session.execute(select(Component.id, Component.code)).all()
        )

        facility_lookup = {
            row.id: {"has_thalassaemia_centre": row.has_thalassaemia_centre}
            for row in session.execute(
                select(Facility.id, Facility.has_thalassaemia_centre)
            ).all()
        }

        print("Building daily panel...")
        panel = build_panel(
            facility_df, component_df, group_df, agg_df, HISTORY_START, HISTORY_END
        )
        panel = add_lag_features(panel)

        print(f"  panel rows: {len(panel):,}")

        print("Classifying demand regimes...")
        states = build_series_states(panel)

        regimes = {}

        for key, state in states.items():
            regime = classify_regime(state["history"][-REGIME_WINDOW_DAYS:])
            regimes[key] = route_regime(
                regime,
                facility_lookup.get(key[0]),
                component_codes.get(key[1], ""),
            )

        print("  regime distribution:")
        for regime, count in Counter(regimes.values()).most_common():
            print(f"    {regime:14s} {count:5d}")

        print("Fitting Pakistan-calendar effects...")
        calendar_factors = fit_calendar_factors(panel)

        print("Training global quantile models...")
        train_df = panel.dropna(subset=FEATURES + ["y"])
        models = train_quantile_models(train_df, FEATURES, seed=SEED)
        print(f"  training rows: {len(train_df):,}  models: {len(models)}")

        future_dates = [
            HISTORY_END + timedelta(days=offset)
            for offset in range(1, HORIZON_DAYS + 1)
        ]

        print("Generating forecasts...")
        lgbm_rows = forecast_lgbm(models, states, regimes, future_dates) if models else {}
        statistical_rows = forecast_statistical(
            states, regimes, future_dates, calendar_factors
        )
        baselines = forecast_baselines(states, future_dates)

        fallback_keys = load_fallback_keys(session)
        print(f"  series on MODEL_FALLBACK from last backtest: {len(fallback_keys)}")

        run_id = new_id()
        rows = []
        version_counts = Counter()

        for key, state in states.items():
            if key in fallback_keys:
                naive = baselines[key]["seasonal_naive"]
                dispersion_ratio = dispersion(state["history"])
                quantile_rows = [
                    count_quantiles(float(value), dispersion_ratio) for value in naive
                ]
                model_version = "MODEL_FALLBACK-seasonal-naive"
            elif key in lgbm_rows:
                quantile_rows = lgbm_rows[key]
                model_version = "lgbm-quantile-v2"
            elif key in statistical_rows:
                quantile_rows, model_version = statistical_rows[key]
            else:
                continue

            version_counts[model_version] += 1

            for horizon_days, target_date in enumerate(future_dates, start=1):
                rows.append(
                    make_row(
                        run_id,
                        key,
                        target_date,
                        horizon_days,
                        quantile_rows[horizon_days - 1],
                        model_version,
                    )
                )

        print("Clearing old forecasts...")
        session.query(Forecast).delete(synchronize_session=False)
        session.flush()

        print("Inserting forecasts...")
        bulk_insert(session, Forecast, rows)
        session.commit()

        print()
        print(f"Forecast rows: {len(rows):,}  run_id {run_id}")
        for version, count in version_counts.most_common():
            print(f"  {version:32s} {count:5d} series")

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


if __name__ == "__main__":
    main()
