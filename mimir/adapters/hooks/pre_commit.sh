#!/bin/bash
# Mimir architectural guardrail — pre-commit hook
# Install: cp mimir/adapters/hooks/pre_commit.sh .git/hooks/pre-commit
#          chmod +x .git/hooks/pre-commit

DIFF=$(git diff --cached --unified=3)
if [ -z "$DIFF" ]; then
    exit 0
fi

RESULT=$(echo "$DIFF" | mimir guardrail check --diff - --rules mimir-rules.yaml --output json 2>/dev/null)
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo ""
    echo "=========================================="
    echo "  Mimir Guardrail: Architectural Violations"
    echo "=========================================="
    echo "$DIFF" | mimir guardrail check --diff - --rules mimir-rules.yaml --output text
    echo ""
    echo "Fix the violations above or use --no-verify to bypass (not recommended)."
    exit 1
fi
