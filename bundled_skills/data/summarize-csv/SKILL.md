---
name: Analyse and summarise a CSV file
slug: summarize-csv
category: data
description: Load a CSV, compute key statistics, and report insights (optionally with a chart).
version: 1
tool_count: 2
---

# Analyse and summarise a CSV file

Use for analysing the contents of a CSV file.

## Steps
1. Use `run_shell` with Python + pandas: load the file, show shape, columns, dtypes.
2. Compute describe() for numerics and value_counts() for key categoricals.
3. Report: what the dataset is, notable distributions, outliers, and any data-quality
   issues (missing values, duplicates).
4. Offer to plot a key chart (matplotlib -> PNG) and/or export findings to PDF.

## Prerequisite
- Needs Python with pandas available to `run_shell`.
