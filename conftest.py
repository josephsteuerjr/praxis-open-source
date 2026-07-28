"""pytest entrypoint for test isolation.

Redirects every module's BASE to a disposable sandbox *before* test collection and
fences the checkout's live tree, so her memory/soul tree is never touched. See
_sandbox.py for the why/how and for the 06.07.2026 run this answers for.
"""
import _sandbox

_sandbox.activate_if_testing()
# Пояс поверх подтяжек: если pytest запущен обёрткой, которую `_started_by_test_runner`
# не узнал, `activate_if_testing` вернёт None и забор не встанет сам по себе.
_sandbox.install_live_tree_guard()
