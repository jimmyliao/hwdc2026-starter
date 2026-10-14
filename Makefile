L ?= L1

.PHONY: setup ask models check report claim run submit test install-hooks uninstall-hooks

setup:
	uv sync

ask:
	uv run hw ask "$(Q)"

models:
	uv run hw models

check:
	uv run hw check "$(L)"

report:
	uv run hw report

claim:
	uv run hw claim

run:
	uv run hw run

submit:
	uv run hw submit $(SUBMIT_ARGS)

test:
	uv run hw test

install-hooks:
	@if git rev-parse --show-toplevel >/dev/null 2>&1; then \
		git config core.hooksPath tools/githooks; echo "已啟用提交前密鑰掃描"; \
	else echo "zip 下載沒有 .git，無法安裝 hook"; fi

uninstall-hooks:
	@if git rev-parse --show-toplevel >/dev/null 2>&1; then \
		git config --unset core.hooksPath || true; echo "已停用自訂 hook"; \
	else echo "zip 下載沒有 .git，無需移除 hook"; fi
