#!/usr/bin/env python
"""下载并校验 ASR 模型（sherpa-onnx）。

模型不入库（``models/*`` 已在 .gitignore）。本脚本与
``bridge/asr/models.py`` 共用同一份清单/校验逻辑，支持：

- HuggingFace 官方源 + hf-mirror 镜像（``--mirror`` 优先镜像，``--base-url`` 任意镜像根）
- 断点续传（``.part`` + HTTP Range；``--no-resume`` 关闭）
- ``--dry-run`` 只打印计划（大小/URL），不写盘、不联网
- ``--verify`` 对已下载模型做完整 sha256 校验
- ``--list`` 列出清单

示例::

    python scripts\\download_models.py --dry-run
    python scripts\\download_models.py                       # 下载全部默认模型
    python scripts\\download_models.py --model sensevoice    # 只下 final 模型
    python scripts\\download_models.py --mirror              # 国内镜像优先
    python scripts\\download_models.py --verify              # 校验已有模型
    python scripts\\download_models.py --base-url https://my.mirror/asr
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_MODELS_PY = REPO_ROOT / "bridge" / "asr" / "models.py"


def load_models_module():
    """按文件路径加载 models.py（不依赖 bridge 包能 import）。"""
    name = "dsh_phone_asr_models"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _MODELS_PY)
    if spec is None or spec.loader is None:  # pragma: no cover - 路径损坏
        raise SystemExit(f"无法加载模型清单: {_MODELS_PY}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _Progress:
    """把 ``(name, downloaded, total)`` 回调渲染成单行进度。"""

    def __init__(self, stream=None, min_interval: float = 0.5) -> None:
        self.stream = stream or sys.stderr
        self.min_interval = min_interval
        self._last = 0.0
        self._last_name = ""
        self._last_line = ""

    def __call__(self, name: str, downloaded: int, total: int) -> None:
        now = time.time()
        finished = total > 0 and downloaded >= total
        if not finished and now - self._last < self.min_interval:
            return
        self._last = now
        if total > 0:
            pct = min(100.0, downloaded * 100.0 / total)
            line = f"  {name}: {pct:5.1f}%  ({downloaded / 1048576:.1f}/{total / 1048576:.1f} MiB)"
        else:
            line = f"  {name}: {downloaded / 1048576:.1f} MiB"
        self._last_line = line
        if name != self._last_name:
            self.stream.write("\n")
            self._last_name = name
        self.stream.write("\r" + line)
        self.stream.flush()
        if finished:
            self.stream.write("\n")
            self._last_name = ""

    def close(self) -> None:
        if self._last_line:
            self.stream.write("\n")
            self.stream.flush()
        self._last_line = ""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="下载/校验 DSH Phone ASR 模型（sherpa-onnx，不入库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--model", "-m", action="append", default=[], metavar="NAME",
                   help="模型名或别名，可重复；默认全部默认模型（streaming + sensevoice）")
    p.add_argument("--models-dir", default=None, metavar="DIR",
                   help="模型根目录（默认 $DSH_PHONE_MODELS_DIR 或 <repo>/models）")
    p.add_argument("--base-url", default=None, metavar="URL",
                   help="自定义下载基地址（优先级最高，例如内网镜像）")
    p.add_argument("--mirror", action="store_true",
                   help="优先使用 hf-mirror.com 镜像")
    p.add_argument("--force", action="store_true", help="已存在也重新下载")
    p.add_argument("--no-resume", action="store_true", help="禁用断点续传")
    p.add_argument("--dry-run", action="store_true", help="只打印下载计划，不下载")
    p.add_argument("--verify", action="store_true", help="只做完整性校验（sha256）")
    p.add_argument("--list", action="store_true", help="列出清单")
    p.add_argument("--json", action="store_true", help="以 JSON 输出汇总")
    p.add_argument("--timeout", type=float, default=60.0, metavar="SEC", help="单请求超时（默认 60s）")
    p.add_argument("--retries", type=int, default=2, metavar="N", help="每个源重试次数（默认 2）")
    return p


def _select_models(mod, names: list[str]) -> list:
    if not names:
        return [mod.MODELS[n] for n in mod.DEFAULT_MODELS]
    specs = []
    for n in names:
        spec = mod.get_spec(n)  # 未知名称 -> UnknownModelError
        if spec not in specs:
            specs.append(spec)
    return specs


def _plan_lines(mod, specs, root: Path, args) -> list[str]:
    lines: list[str] = []
    for spec in specs:
        plan = mod.download_plan(spec, root, base_url=args.base_url, force=args.force)
        state = "已就绪" if not plan else f"需下载 {len(plan)} 个文件"
        lines.append(
            f"- {spec.name} [{spec.kind}] -> {mod.model_dir(spec, root)}"
            f"  总大小 {mod.format_bytes(spec.total_size)}  ({state})"
        )
        for item in plan:
            lines.append(f"    * {item.file.rel_path}  {mod.format_bytes(item.size)}")
            for url in item.urls[:2]:
                lines.append(f"        {url}")
            if len(item.urls) > 2:
                lines.append(f"        ...（共 {len(item.urls)} 个候选源）")
    return lines


def _mirror_base(mod, spec, args) -> str | None:
    """--mirror 时返回 hf-mirror 基地址；否则用 --base-url。"""
    if args.base_url:
        return args.base_url
    if args.mirror and spec.hf_repo:
        return f"https://hf-mirror.com/{spec.hf_repo}/resolve/main"
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mod = load_models_module()

    if args.list:
        print(f"模型清单（{_MODELS_PY}）:")
        for name, spec in mod.MODELS.items():
            print(
                f"- {name}  [{spec.kind}]  {mod.format_bytes(spec.total_size)}\n"
                f"    {spec.description}\n"
                f"    目录: {spec.dir_name()}  仓库: {spec.hf_repo or '-'}"
            )
            for f in spec.files:
                print(f"      {f.rel_path}  {mod.format_bytes(f.size)}  sha256={f.sha256[:16]}…")
        return 0

    try:
        specs = _select_models(mod, args.model)
    except mod.UnknownModelError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    root = mod.resolve_models_dir(args.models_dir)

    if args.verify:
        bad = 0
        results = []
        for spec in specs:
            check = mod.verify_model(spec, root)
            results.append({
                "model": spec.name,
                "directory": str(check.directory),
                "ok": check.ok,
                "problems": check.problem_lines(),
            })
            status = "OK" if check.ok else "FAIL: " + "; ".join(check.problem_lines())
            print(f"- {spec.name}: {status}")
            if not check.ok:
                bad += 1
        if args.json:
            print(json.dumps({"modelsDir": str(root), "results": results}, ensure_ascii=False, indent=2))
        return 0 if bad == 0 else 1

    plan_lines = _plan_lines(mod, specs, root, args)
    total = sum(spec.total_size for spec in specs)
    print(f"模型目录: {root}")
    print("\n".join(plan_lines) if plan_lines else "- （无可下载内容）")
    print(f"总计（清单大小）: {mod.format_bytes(total)}")

    if args.dry_run:
        print("\n--dry-run: 仅显示计划，未下载任何文件。")
        return 0

    progress = _Progress()
    summary = []
    t0 = time.time()
    try:
        for spec in specs:
            base = _mirror_base(mod, spec, args)
            print(f"\n>>> 下载 {spec.name} ({mod.format_bytes(spec.total_size)}) ...")
            mod.download_model(
                spec,
                root,
                base_url=base,
                force=args.force,
                resume=not args.no_resume,
                timeout=args.timeout,
                retries=args.retries,
                progress=progress,
            )
            print(f">>> {spec.name} 完成（sha256 校验通过）")
            summary.append({"model": spec.name, "directory": str(mod.model_dir(spec, root)), "ok": True})
    except KeyboardInterrupt:
        print("\n已中断；可用相同命令继续（.part 断点续传）。", file=sys.stderr)
        return 130
    except mod.ModelError as exc:
        print(f"\n错误: {exc}", file=sys.stderr)
        return 1
    finally:
        progress.close()

    elapsed = time.time() - t0
    print(f"\n全部完成: {len(summary)} 个模型，用时 {elapsed:.1f}s")
    if args.json:
        print(json.dumps({"modelsDir": str(root), "results": summary, "elapsedSec": round(elapsed, 1)},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
