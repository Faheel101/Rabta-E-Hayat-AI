# Synthetic mock-data package

The separate `Rabta-e-Hayat_Synthetic_Data.zip` submission artifact contains a validated prebuilt SQLite database for judges who do not want to wait for the full deterministic rebuild.

## Snapshot

- Scenario time: 6 August 2026, 08:00 PKT
- Organizations: 10
- Facilities: 30
- Active demo users: 15
- Synthetic donors: 7,460
- Blood units: 36,899
- Forecast rows: 43,200
- Latest transfer plan: 189 recommendations
- Retained history: 1 July 2025 through 5 August 2026

All identities and records are synthetic. The archive contains no real donor, patient, hospital or credential data.

## Use

Extract the archive, verify `SHA256SUMS.txt`, and point the uncommitted `.env` file at the extracted database without overwriting any existing data:

```dotenv
DATABASE_URL=sqlite:////absolute/path/to/rabta.db
```

Then start the application and run:

```bash
python -m scripts.release_check
```

The shared synthetic demo password is `Rabta@2026`; it must never be reused outside this demonstration.

The compact judge package retains more than a year of history and all current operational, forecast, risk and transfer state. The full 547-day scenario can be regenerated from source with `python -m scripts.rebuild`.

## Verified artifact

- Archive: `Rabta-e-Hayat_Synthetic_Data.zip`
- Compressed size: 117,266,637 bytes (approximately 112 MiB)
- ZIP SHA-256: `81c6f224a199239c69e3385bcfd634dadbb2349b9fa6e02ffea978aa18ca65ec`
- Validation: ZIP integrity passed, application release check passed, and 169/169 live role/permission probes passed against the packaged database.
