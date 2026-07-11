from __future__ import annotations

import argparse
import shutil
import os
from datetime import date
from pathlib import Path
import sys

from .agent_runner import run_agent
from .config import ProjectPaths, RunContext, load_dotenv, get_market_date


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Qwen-Agent Stock Daily Report Skill Runner — 使用 agent 工具调用复现 SKILL.md 日报流程",
    )
    p.add_argument("ticker", help="yfinance ticker，例如 ORCL, BTC-USD, QQQ, 0700.HK, ^NDX")
    p.add_argument("-m", "--months", type=int, default=3, help="K线图月份数，默认 3")
    p.add_argument("-o", "--output", help="输出 HTML 文件路径。默认写到 runs/<ticker>_<date>/")
    p.add_argument("--model", default=None, help="主 Agent 模型名；默认读取 LLM_MODEL/QWEN_MODEL。provider=deepseek 时默认 deepseek-v4-flash，否则 qwen-plus")
    p.add_argument("--provider", choices=["auto", "dashscope", "deepseek", "openai_compatible"], default=None, help="主 Agent 模型提供商；默认读取 LLM_PROVIDER/auto，若有有效 DEEPSEEK_API_KEY 则优先 deepseek，否则 dashscope")
    p.add_argument("--run-dir", help="中间文件目录，默认 runs/<ticker>_<date>")
    p.add_argument("--date", default=get_market_date(), help="报告日期，默认美东市场日期")
    p.add_argument("--min-notes", type=int, default=10, help="消息面 notes 最少条数，默认 10")
    p.add_argument("--no-web-tools", action="store_true", help="不加载 Qwen-Agent 内置 web_search/web_extractor 工具；SearXNG 和 DashScope 回退工具仍可用")
    p.add_argument("--searxng-url", help="临时覆盖 .env 中的 SEARXNG_URL，例如 http://127.0.0.1:8080")
    p.add_argument("--searxng-language", help="临时覆盖 SEARXNG_LANGUAGE；建议 auto/en-US/zh-CN，默认 auto")
    p.add_argument("--searxng-time-range", choices=["day", "month", "year", ""], help="临时覆盖 SEARXNG_TIME_RANGE，默认 month")
    p.add_argument("--searxng-engines", help="临时覆盖 SEARXNG_ENGINES，例如 google 或 google,google news；为空则使用实例默认引擎")
    p.add_argument("--search-provider", choices=["auto", "priority", "searxng", "serper", "both"], help="V5.8 搜索来源：priority/serper/searxng/both/auto；默认 auto，即生产模式 Serper-first")
    p.add_argument("--serper-api-key", help="临时覆盖 SERPER_API_KEY，用于测试 Serper 搜索质量")
    p.add_argument("--serper-types", help="临时覆盖 SERPER_TYPES，例如 search 或 search,news")
    p.add_argument("--no-article-fetch", action="store_true", help="禁用 v5.8 正文抓取增强层，仅使用搜索 title/snippet")
    p.add_argument("--article-fetch-max-urls", type=int, help="临时覆盖 ARTICLE_FETCH_MAX_URLS")
    p.add_argument("--quiet", action="store_true", help="不流式打印 agent 输出")
    p.add_argument("--keep-intermediate", action="store_true", help="保留中间文件。默认也会保留 run-dir 中文件；此参数预留给后续清理策略")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    paths = ProjectPaths.from_root(root)
    load_dotenv(root)

    provider = args.provider or os.environ.get("LLM_PROVIDER") or os.environ.get("MODEL_PROVIDER") or "auto"
    deepseek_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    deepseek_key_ok = bool(deepseek_key and not deepseek_key.lower().startswith("your_"))
    if provider == "auto":
        provider = "deepseek" if deepseek_key_ok else "dashscope"
    if provider == "deepseek":
        model_name = args.model or os.environ.get("LLM_MODEL") or os.environ.get("DEEPSEEK_MODEL") or "deepseek-v4-flash"
    else:
        model_name = args.model or os.environ.get("LLM_MODEL") or os.environ.get("QWEN_MODEL", "qwen-plus")

    if args.searxng_url:
        os.environ["SEARXNG_URL"] = args.searxng_url
    if args.searxng_language:
        os.environ["SEARXNG_LANGUAGE"] = args.searxng_language
    if args.searxng_time_range is not None:
        os.environ["SEARXNG_TIME_RANGE"] = args.searxng_time_range
    if args.searxng_engines is not None:
        os.environ["SEARXNG_ENGINES"] = args.searxng_engines
    if args.search_provider:
        os.environ["SEARCH_PROVIDER"] = args.search_provider
    if args.serper_api_key:
        os.environ["SERPER_API_KEY"] = args.serper_api_key
    if args.serper_types:
        os.environ["SERPER_TYPES"] = args.serper_types
    if args.no_article_fetch:
        os.environ["ARTICLE_FETCH_ENABLED"] = "false"
    if args.article_fetch_max_urls is not None:
        os.environ["ARTICLE_FETCH_MAX_URLS"] = str(args.article_fetch_max_urls)

    ticker_safe = args.ticker.upper().replace("-", "_").replace("^", "IDX_").replace(".", "_")
    run_dir = Path(args.run_dir).resolve() if args.run_dir else (root / "runs" / f"{ticker_safe}_{args.date}").resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    output = Path(args.output).resolve() if args.output else None
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)

    ctx = RunContext(
        paths=paths,
        ticker=args.ticker.upper(),
        months=args.months,
        run_dir=run_dir,
        report_date=args.date,
        output_html=output,
        min_notes=args.min_notes,
        keep_intermediate=args.keep_intermediate,
    )

    print("=" * 72)
    print("Qwen-Agent Stock Daily Report Skill Runner")
    print(f"Ticker: {ctx.ticker} | Provider: {provider} | Model: {model_name} | Date: {ctx.report_date}")
    print(f"Run dir: {ctx.run_dir}")
    print("=" * 72)

    try:
        result = run_agent(
            ctx=ctx,
            model=model_name,
            provider=provider,
            enable_builtin_web=not args.no_web_tools,
            verbose=not args.quiet,
        )
    except Exception as exc:
        print(f"\n[FAILED] Agent 运行失败: {exc}", file=sys.stderr)
        return 1

    print("\n" + "=" * 72)
    if result.ok:
        print("[SUCCESS] 日报生成完成")
        print(f"HTML: {result.output_html}")
        print(f"中间文件目录: {result.run_dir}")
        return 0
    print("[FAILED] Agent 未生成有效 HTML")
    print(f"请检查中间文件目录: {result.run_dir}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
