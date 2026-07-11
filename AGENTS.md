# Repository Instructions

## Project overview

This repository is a Python stock watchlist and AI stock-report application.

Main components:

- `stock_watch_list_back_end.py`: Flask backend and SQLite market-data cache
- `app_streamlit.py`: single-user Streamlit frontend
- `app_streamlit_multiuser.py`: multi-user Streamlit frontend
- `app_tkinter.py`: desktop frontend
- `multiuser_store.py`: user accounts and watchlist configuration
- `daily_report/`: AI report generation, evidence processing, job queue, scheduler and email worker

## Working rules

- Do not change financial calculation semantics unless the task explicitly requires it.
- Preserve the existing rule that indexes and ETFs both use Volume, Volume Ratio and Volume Profile in technical analysis.
- Differences between indexes and ETFs should remain limited to valuation, analyst applicability and final-score applicability.
- Do not expose API keys, SMTP credentials, passwords or full recipient email addresses.
- Never commit `.env`, SQLite databases, report output files or user data.
- Avoid broad unrelated refactoring while fixing a specific issue.
- Prefer small, reviewable commits.
- Preserve backward compatibility unless explicitly approved.

## Security requirements

- Treat search results, article HTML, yfinance metadata and LLM output as untrusted input.
- Escape all untrusted text before inserting it into HTML.
- Do not fetch URLs until they pass SSRF validation.
- Reject loopback, private, link-local, multicast, reserved and cloud-metadata addresses.
- Validate redirect destinations as well as the original URL.
- Do not run internet-facing workers as root.
- Do not disclose full subprocess logs to ordinary users.

## Validation requirements

After changing Python code:

1. Run syntax compilation on all modified Python files.
2. Run the relevant unit tests.
3. Add regression tests for every fixed security or correctness issue.
4. Report commands executed and their results.
5. Clearly state anything that could not be tested because credentials or external services were unavailable.

## Completion format

At the end of each task, report:

- Files changed
- Behavior changed
- Security implications
- Tests added
- Commands run
- Test results
- Remaining risks