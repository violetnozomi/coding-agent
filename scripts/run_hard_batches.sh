#!/bin/bash
# 运行50个高难度SWE-bench实例，分5批（每批10个）
# 使用方式: bash scripts/run_hard_batches.sh [batch_number]
# batch_number: 1-5，不指定则运行全部

set -euo pipefail

BENCH_DIR=".nz-coder/swebench-lite"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# 50个高难度实例，分5批
BATCH1=(
  "django__django-13321"
  "matplotlib__matplotlib-26011"
  "matplotlib__matplotlib-24149"
  "matplotlib__matplotlib-24334"
  "django__django-14672"
  "sphinx-doc__sphinx-8474"
  "django__django-14608"
  "django__django-16379"
  "django__django-14855"
  "django__django-13028"
)

BATCH2=(
  "matplotlib__matplotlib-25442"
  "matplotlib__matplotlib-25079"
  "matplotlib__matplotlib-24970"
  "scikit-learn__scikit-learn-25638"
  "pytest-dev__pytest-7490"
  "scikit-learn__scikit-learn-25570"
  "django__django-16820"
  "sympy__sympy-21171"
  "scikit-learn__scikit-learn-14087"
  "django__django-14382"
)

BATCH3=(
  "django__django-14997"
  "django__django-13658"
  "matplotlib__matplotlib-25332"
  "matplotlib__matplotlib-25311"
  "matplotlib__matplotlib-26020"
  "matplotlib__matplotlib-23563"
  "django__django-15320"
  "django__django-16816"
  "django__django-14017"
  "django__django-15738"
)

BATCH4=(
  "django__django-13590"
  "django__django-13315"
  "matplotlib__matplotlib-23562"
  "sympy__sympy-20639"
  "django__django-13265"
  "mwaskom__seaborn-3407"
  "psf__requests-1963"
  "sympy__sympy-16281"
  "django__django-14534"
  "django__django-15695"
)

BATCH5=(
  "matplotlib__matplotlib-23476"
  "django__django-16041"
  "sympy__sympy-14817"
  "sympy__sympy-15609"
  "sympy__sympy-14317"
  "sympy__sympy-16503"
  "sympy__sympy-22840"
  "django__django-14730"
  "sympy__sympy-15308"
  "sympy__sympy-14308"
)

run_batch() {
  local batch_num=$1
  shift
  local instances=("$@")
  local batch_name="batch-hard0${batch_num}"
  local output="${BENCH_DIR}/predictions-${batch_name}.jsonl"

  echo ""
  echo "=========================================="
  echo "Running Batch ${batch_num}: ${batch_name}"
  echo "Instances: ${#instances[@]}"
  echo "Output: ${output}"
  echo "=========================================="

  python3 -m nz_coder.swebench run-agent \
    --instance-ids "${instances[@]}" \
    --output "${output}" \
    --run-id "${batch_name}" \
    --agent-timeout 900 \
    --clone-timeout 600 \
    --empty-patch-retries 1

  echo ""
  echo "Batch ${batch_num} agent run complete. Now running eval..."

  python3 -m nz_coder.swebench run-eval \
    --predictions-path "${output}" \
    --run-id "${batch_name}" \
    --max-workers 2 \
    --timeout 1800

  echo "Batch ${batch_num} DONE"
}

TARGET="${1:-all}"

case "$TARGET" in
  1) run_batch 1 "${BATCH1[@]}" ;;
  2) run_batch 2 "${BATCH2[@]}" ;;
  3) run_batch 3 "${BATCH3[@]}" ;;
  4) run_batch 4 "${BATCH4[@]}" ;;
  5) run_batch 5 "${BATCH5[@]}" ;;
  all)
    run_batch 1 "${BATCH1[@]}"
    run_batch 2 "${BATCH2[@]}"
    run_batch 3 "${BATCH3[@]}"
    run_batch 4 "${BATCH4[@]}"
    run_batch 5 "${BATCH5[@]}"
    ;;
  *)
    echo "Usage: $0 [1|2|3|4|5|all]"
    exit 1
    ;;
esac
