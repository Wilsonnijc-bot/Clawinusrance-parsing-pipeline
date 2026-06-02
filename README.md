# AI Agent for Insurance Sales

## Project Overview

Insurance agents spend a lot of time answering repeated client questions on WhatsApp and WeChat, especially when they need to check plan details, pricing, coverage, and requirements. This project is an AI assistant that helps agents draft accurate replies faster by using structured insurance product knowledge.

This code repository is the backend data pipeline for that assistant. It collects insurance product information from product pages and brochures, cleans it into structured CSV/JSON records, and syncs it to Supabase/PostgreSQL so the assistant can use it in a RAG workflow.

## Outcome / Impact

- 20+ paying users.
- Users from insurance firms including Prudential and AIA.
- Users reported saving 2+ hours per day.
- Generated real revenue.
- Iterated through real user feedback.
- Docker-deployed for real product usage.

## Key Features

- RAG-ready insurance product knowledge pipeline.
- Product discovery from insurer listing pages.
- Brochure extraction and field enrichment.
- Human-readable insurance product fields, including coverage, pricing, age range, customer requirements, and plan structure.
- JSON artifact generation for inspection and debugging.
- CSV normalization for local workflows.
- Supabase/PostgreSQL upsert for cloud storage.
- Docker-based deployment in the product environment.

## Tech Stack

### Frontend

- Not included in this repository.
- This repository contains the backend/data pipeline that powers the assistant's insurance knowledge base.

### Backend / Pipeline

- Python 3.
- CLI runner with `argparse`.
- Parallel execution with `concurrent.futures`.
- CSV and JSON processing with Python standard-library modules.
- Main orchestration script: `run_full_pipeline.py`.

### AI / LLM

- Codex CLI child-agent runs for extraction and enrichment tasks.
- OpenAI-compatible API configuration in `api_configgg.py`.
- Prompt files in `prompts/`.
- RAG workflow support through structured insurance product data generation.

### Web Extraction

- Generated parser scripts in `parser_outputs/`.
- `requests`.
- `BeautifulSoup` / `bs4`.
- Product-page URL parsing and validation.
- Brochure URL discovery and enrichment.

### Database / Storage

- Supabase PostgreSQL.
- `psycopg` for PostgreSQL access.
- CSV seed/export files, including `Insurance_datas.csv`, `productpageurl.csv`, and `dental_insurance.csv`.
- JSON outputs in `json_outputs/`.

### Deployment

- Docker deployment was used for the product rollout.
- This repository snapshot does not include a committed `Dockerfile` or `docker-compose.yml`.
- The pipeline can be run inside a Docker image that has Python, the required Python packages, and the Codex CLI installed.

## Quick Start

### 1. Run with Docker

Use this command pattern when running from the deployed Docker image or from an image built with this repository's Python dependencies and Codex CLI:

```bash
docker run --rm -it \
  -e SUPABASE_DB_URL='postgresql://postgres:[YOUR-PASSWORD]@db.[YOUR-PROJECT].supabase.co:5432/postgres' \
  -e OPENAI_API_KEY='[YOUR-OPENAI-API-KEY]' \
  -v "$PWD:/app" \
  -w /app \
  insurance-sales-agent:latest \
  python3 run_full_pipeline.py --flow product-page
```

Run the brochure-seed flow:

```bash
docker run --rm -it \
  -e SUPABASE_DB_URL='postgresql://postgres:[YOUR-PASSWORD]@db.[YOUR-PROJECT].supabase.co:5432/postgres' \
  -e OPENAI_API_KEY='[YOUR-OPENAI-API-KEY]' \
  -v "$PWD:/app" \
  -w /app \
  insurance-sales-agent:latest \
  python3 run_full_pipeline.py --flow category-specific --category-csv dental_insurance.csv
```

### 2. Run Locally

Install the visible Python dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Configure the database:

```bash
export SUPABASE_DB_URL='postgresql://postgres:[YOUR-PASSWORD]@db.[YOUR-PROJECT].supabase.co:5432/postgres'
```

Make sure the Codex CLI is installed and available on `PATH`, then run:

```bash
python3 run_full_pipeline.py --flow product-page
```

Or run the brochure-seed flow:

```bash
python3 run_full_pipeline.py --flow category-specific --category-csv dental_insurance.csv
```

## Pipeline Flows

### Flow 1: Product-Page Flow

Purpose: start from listing page URLs, discover products, enrich product rows with brochure information, and persist the final rows into Supabase/PostgreSQL.

Entry input:

- `productpageurl.csv`

Main command:

```bash
export SUPABASE_DB_URL='postgresql://postgres:[YOUR-PASSWORD]@db.[YOUR-PROJECT].supabase.co:5432/postgres'
python3 run_full_pipeline.py --flow product-page
```

Bootstrap the existing local insurance CSV into Supabase:

```bash
export SUPABASE_DB_URL='postgresql://postgres:[YOUR-PASSWORD]@db.[YOUR-PROJECT].supabase.co:5432/postgres'
python3 sync_insurance_to_supabase.py --csv Insurance_datas.csv
```

Main scripts involved:

- `run_full_pipeline.py --flow product-page`
- `insurance_db.py`
- `sync_insurance_to_supabase.py`
- `codex_self_loop.py`
- `normalizer1.py`
- `normalizers/normalizer1.py`
- `brochure_round2_loop.py`
- `normalizer2.py`
- `normalizers/normalizer2.py`

Source of truth:

- Supabase/PostgreSQL table `insurance_products`

Produced artifacts:

- `parser_outputs/parser_<site>_row_<n>.py`
- `json_outputs/insurance_rows_<site>_row_<n>.json`
- `json_outputs/brochure_fields_row_<n>.json`
- `json_outputs/Insurance_datas_brochure_parsing.json`
- Run-local working CSV exported from Supabase

Flow:

1. Read listing URLs from `productpageurl.csv`.
2. Export the current `insurance_products` table into a run-local working CSV.
3. Run round 1 with `codex_self_loop.py`.
4. Generate parser files and `insurance_rows_*.json` artifacts.
5. Normalize round-1 artifacts into the run-local working CSV.
6. Run round 2 only on rows newly added by round 1.
7. Normalize brochure fields back into the run-local working CSV.
8. Upsert the final affected rows into Supabase.

Mental model:

```text
productpageurl.csv -> Supabase export -> round 1 -> round 2 -> run-local working CSV -> Supabase upsert
```

### Flow 2: Brochure-Seed Flow

Purpose: start from an existing row-shaped CSV, treat each existing row as seed truth, use `Product_Brochure_route` as the brochure source, enrich the remaining fields, and sync selected rows into Supabase/PostgreSQL.

Entry input:

- Any local row-shaped CSV that already contains product rows.
- Examples: `dental_insurance.csv`, `car_insurance.csv`, `flight_insurance.csv`.
- Required seed column: `Product_Brochure_route`.

Main command:

```bash
export SUPABASE_DB_URL='postgresql://postgres:[YOUR-PASSWORD]@db.[YOUR-PROJECT].supabase.co:5432/postgres'
python3 run_full_pipeline.py --flow category-specific --category-csv dental_insurance.csv
```

Run on a row window:

```bash
export SUPABASE_DB_URL='postgresql://postgres:[YOUR-PASSWORD]@db.[YOUR-PROJECT].supabase.co:5432/postgres'
python3 run_full_pipeline.py --flow category-specific --category-csv dental_insurance.csv --start-index 1 --limit 20
```

Bootstrap a local brochure-seed CSV into its cloud table:

```bash
export SUPABASE_DB_URL='postgresql://postgres:[YOUR-PASSWORD]@db.[YOUR-PROJECT].supabase.co:5432/postgres'
python3 sync_csv_to_supabase.py --csv dental_insurance.csv --table-name dental_insurance
```

How the cloud table is chosen:

- If `--supabase-table` is not passed, the table defaults to the local CSV stem.
- `dental_insurance.csv` maps to `dental_insurance`.
- `car_insurance.csv` maps to `car_insurance`.
- `flight_insurance.csv` maps to `flight_insurance`.
- To use a different table, pass `--supabase-table <table_name>`.

Main scripts involved:

- `run_full_pipeline.py --flow category-specific`
- `insurance_db.py`
- `sync_csv_to_supabase.py`
- `brochure_round2_loop.py`
- `normalizer2.py`
- `normalizers/normalizer2.py`

Produced artifacts:

- `json_outputs/brochure_fields_row_<n>.json`
- `json_outputs/<category_csv_stem>_brochure_parsing.json`

Flow:

1. Read existing product rows from the local category CSV.
2. Copy that CSV into a run-local working CSV.
3. Select rows from that local seed CSV.
4. For each selected row, use `Product_Brochure_route` as the brochure parsing source and `URL` as supporting context.
5. Run round-2 brochure extraction for the selected row window.
6. Skip rows whose `Product_Brochure_route` is empty.
7. Write brochure outputs to JSON artifacts.
8. Normalize brochure fields back into the run-local working CSV.
9. Upsert the selected final rows into Supabase.

Mental model:

```text
<local_seed_csv with Product_Brochure_route> -> run-local working CSV -> round 2 -> <cloud_table>
```

## Main Runner

`run_full_pipeline.py` is the main flow selector.

Supported modes:

- `--flow product-page`
- `--flow category-specific`

Useful arguments:

- `--insurance-datas-csv`: target CSV name for the product-page flow. Defaults to `Insurance_datas.csv`.
- `--category-csv`: local seed CSV for the brochure-seed flow. Defaults to `dental_insurance.csv`.
- `--supabase-table`: optional Supabase/PostgreSQL table override.
- `--brochure-parsing-json`: optional consolidated round-2 JSON output path.
- `--start-index`: row start for the brochure-seed flow.
- `--limit`: row count limit for the brochure-seed flow.
- `--reparse-second-round`: force round 2 to reprocess all selected rows.

## Important Files

- `run_full_pipeline.py`: main pipeline runner.
- `codex_self_loop.py`: round-1 product-page extraction runner.
- `brochure_round2_loop.py`: round-2 brochure enrichment runner.
- `normalizers/normalizer1.py`: maps round-1 extraction artifacts into CSV rows.
- `normalizers/normalizer2.py`: maps brochure-enrichment artifacts into CSV rows.
- `insurance_db.py`: shared Supabase/PostgreSQL sync layer.
- `sync_insurance_to_supabase.py`: bootstrap helper for `Insurance_datas.csv`.
- `sync_csv_to_supabase.py`: bootstrap helper for row-shaped category CSVs.
- `productpageurl.csv`: listing-page seed file.
- `Insurance_datas.csv`: local bootstrap/export artifact for the product-page flow.
- `dental_insurance.csv`: example brochure-seed CSV.
- `prompts/`: extraction prompts.
- `json_outputs/`: generated extraction and enrichment artifacts.
- `parser_outputs/`: generated product-page parser scripts.

## Database Configuration

Both Supabase-backed flows require:

```bash
export SUPABASE_DB_URL='postgresql://postgres:[YOUR-PASSWORD]@db.[YOUR-PROJECT].supabase.co:5432/postgres'
```

The sync layer can target any compatible table. Existing examples include:

- `insurance_products`
- `dental_insurance`
- `car_insurance`
- `flight_insurance`

Tables use these business fields in `snake_case`:

- `plan_id`
- `plan_name`
- `provider_company`
- `url`
- `product_brochure_route`
- `plan_category`
- `coverage_description`
- `pricing`
- `age`
- `customer_requirement`
- `price_structure`
- `additional_informations`
- `last_updated`

The database layer also creates:

- Primary key: `id`
- Timestamps: `created_at`, `updated_at`
- Unique business key: `(url, plan_name)`

## Notes

- `Insurance_datas.csv` is a local bootstrap/export artifact. Once Supabase is in use, Supabase/PostgreSQL is the product-page flow source of truth.
- Rows without `Product_Brochure_route` are skipped by round 2.
- The repository currently stores generated parser and JSON artifacts, which are useful for inspection and debugging.
- API keys should be managed through environment variables or deployment secrets in production.
