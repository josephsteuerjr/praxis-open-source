# -*- coding: utf-8 -*-
"""Полноэкранные 3D-сцены мини-аппа: жест, кадр, слой, «назад», доставка.

Тесты текстовые, потому что проверяемое — статика оболочки: JS и CSS на прод
уезжают файлами, их не во что импортировать. Каждый тест целится в МЕХАНИЗМ,
из-за которого дефект был возможен, а не в наличие слова в файле.
"""
import ast
import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATIC = BASE / "praxis_static"


def read(rel):
    return (BASE / rel).read_text(encoding="utf-8")


def css_rule(css, selector):
    "Тело правила по ТОЧНОМУ селектору: `.mem-canvas` и `.stage-full .mem-canvas` — разные."
    head = "\n" + selector + " {"
    at = css.find(head)
    if at < 0:
        return ""
    end = css.find("\n}", at)
    return css[at + len(head):end]


def js_block(source, header, closer="\n  }"):
    "Тело функции/метода от заголовка до его закрывающей скобки на том же отступе."
    at = source.index(header)
    end = source.index(closer, at)
    return source[at:end]


class TestSceneGestureBelongsToPageWhenCollapsed(unittest.TestCase):
    """Свёрнутая сцена обязана отдавать вертикаль странице, развёрнутая — забирать.

    ⚠ 03.08.2026. Коммит 4c77776c поставил на .mem-canvas `touch-action: none`, но
    JS-гейт в обеих сценах остался прежним и продолжал БРОСАТЬ вертикальную
    протяжку «в пользу страницы». Страница её уже не получала. Итог: вертикальный
    свайп по сцене не делал вообще ничего. Тесты проверяют обе половины размена —
    порознь каждая снова даст ту же немоту.
    """

    def test_collapsed_canvas_leaves_vertical_axis_to_the_page(self):
        body = css_rule(read("praxis_static/app.css"), ".mem-canvas")
        self.assertTrue(body, "правило .mem-canvas не найдено")
        self.assertIn("touch-action: pan-y", body)
        self.assertNotIn("touch-action: none", body)

    def test_immersive_canvas_takes_every_gesture(self):
        body = css_rule(read("praxis_static/app.css"), ".stage-full .mem-canvas")
        self.assertTrue(body, "правило .stage-full .mem-canvas не найдено")
        self.assertIn("touch-action: none", body)

    def test_space3d_gate_lets_vertical_through_only_when_immersive(self):
        js = read("praxis_static/space3d.js")
        self.assertIn(
            "if (gesture.touch && !this.immersive "
            "&& Math.abs(totalDx) <= Math.abs(totalDy)) return;",
            js,
        )

    def test_flat_fallback_gate_lets_vertical_through_only_when_immersive(self):
        js = read("praxis_static/memory_views.js")
        self.assertIn(
            "if (gesture.touch && !this.immersive "
            "&& Math.abs(totalDx) <= Math.abs(totalDy)) return;",
            js,
        )

    def test_both_scenes_can_switch_the_mode(self):
        for rel in ("praxis_static/space3d.js", "praxis_static/memory_views.js"):
            self.assertIn("setImmersive(on) {", read(rel), rel)


class TestSceneRefitsFrameAfterResize(unittest.TestCase):
    """Кадрирование пересчитывается и тогда, когда камеру уже двигали.

    ⚠ fitDistance («дистанция, с которой граф целиком влезает в кадр») считался
    только в recenter(), а recenter() при ресайзе звался лишь при userMoved=false.
    Покрученная сцена разворачивалась на весь экран с рамкой от полосы в 320 px.
    """

    def test_space3d_resize_refits_instead_of_doing_nothing(self):
        js = read("praxis_static/space3d.js")
        self.assertIn("  _refit() {", js)
        body = js_block(js, "  _onResize() {")
        self.assertIn("if (!this.userMoved) this.recenter();", body)
        self.assertIn("else this._refit();", body)

    def test_space3d_refit_keeps_the_camera_where_the_owner_put_it(self):
        body = js_block(read("praxis_static/space3d.js"), "  _refit() {")
        for forbidden in ("this.dolly", "this.userMoved", "this.yaw", "this.pitch"):
            self.assertNotIn(forbidden, body, "_refit не имеет права трогать камеру")

    def test_flat_fallback_keeps_the_same_world_point_centred(self):
        js = read("praxis_static/memory_views.js")
        at = js.index("class Constellation2D")
        body = js_block(js[at:], "  resize() {")
        self.assertIn("const prevWidth = this.width;", body)
        self.assertIn("this.offsetX += (this.width - prevWidth) / 2;", body)
        self.assertIn("this.offsetY += (this.height - prevHeight) / 2;", body)


class TestFullscreenLayerLivesOutsideTheScrollingView(unittest.TestCase):
    """Слой обязан быть внутри #app, но вне `.view`, и лежать между доком и шторкой.

    ⚠ У `.view` есть transform и will-change: трансформированный предок становится
    containing block даже для position:fixed, поэтому класс на .mem-stage дал бы
    «фуллскрин» размером с секцию. Вне #app слой не получил бы inert от openSheet.
    """

    def test_shell_hosts_the_layer_inside_app_and_after_the_views(self):
        html = read("praxisapp.html")
        host = html.index('<div id="stageFullscreen"')
        nav = html.index("</nav>")
        scrim = html.index('<div id="scrim"')
        self.assertLess(nav, host, "хост обязан стоять после навигации, вне секций")
        self.assertLess(host, scrim, "хост обязан лежать внутри #app, до #scrim")
        self.assertIn("</div>", html[host:scrim], "между хостом и #scrim нет закрытия #app")

    def test_layer_is_fixed_respects_safe_area_and_hides_by_attribute(self):
        css = read("praxis_static/app.css")
        body = css_rule(css, ".stage-full")
        self.assertTrue(body, "правило .stage-full не найдено")
        self.assertIn("position: fixed", body)
        self.assertIn("var(--safe-top)", body)
        # display:flex перебивает атрибут hidden — без отдельного правила слой висел бы всегда
        self.assertIn("display: none", css_rule(css, ".stage-full[hidden]"))

    def test_layer_sits_above_the_dock_and_below_the_sheet(self):
        css = read("praxis_static/app.css")
        found = re.search(r"z-index:\s*(\d+)", css_rule(css, ".stage-full"))
        self.assertTrue(found, "у .stage-full нет z-index")
        z = int(found.group(1))
        self.assertGreater(z, 32, "слой обязан перекрыть док (32) и топбар (30)")
        self.assertLess(z, 60, "досье узла обязано открываться НАД развёрнутой сценой")

    def test_scene_leaves_a_spacer_of_its_own_height(self):
        # Без распорки лента схлопывается на высоту уехавшей сцены, прокрутка уезжает,
        # и после сворачивания владелец оказывается не там, где был.
        self.assertIn("height: var(--mem-stage-h)",
                      css_rule(read("praxis_static/app.css"), ".mem-stage-slot"))
        js = read("praxis_static/memory_views.js")
        self.assertIn('el("div", "mem-stage-slot")', js)
        self.assertIn("slot.parentNode.replaceChild(stage, slot)", js)


class TestFullscreenDoorsAndExits(unittest.TestCase):
    """Двери внутрь и наружу: тап по пустому, кнопка, «назад», уход из раздела."""

    def test_empty_tap_fires_only_when_there_is_nothing_to_deselect(self):
        # Тап по пустому месту уже занят снятием выделения: повесь разворот раньше —
        # и один тап делал бы два дела сразу.
        js = read("praxis_static/space3d.js")
        deselect = js.index("} else if (this.selected) {")
        expand = js.index("} else if (this.onEmptyTap) {")
        self.assertLess(deselect, expand)

    def test_expand_button_shares_a_holder_with_recenter(self):
        # Шапка блока — flex со space-between: вторая кнопка в ней напрямую разводит
        # заголовок и кнопки по краям.
        js = read("praxis_static/memory_views.js")
        self.assertIn("tools.append(constellation.recenter, constellation.expand)", js)
        self.assertIn("tools.append(codeSpace.recenter, codeSpace.expand)", js)
        self.assertNotIn("head.append(constellation.recenter)", js)
        self.assertNotIn("head.append(codeSpace.recenter)", js)
        self.assertIn("display: flex", css_rule(read("praxis_static/app.css"), ".mem-block__tools"))

    def test_back_closes_the_sheet_first_and_the_scene_before_the_history(self):
        # Досье узла открывается ПОВЕРХ развёрнутой сцены. Переставь ветки — и
        # владелец уедет из фуллскрина в предыдущий вид, а сцена останется висеть.
        body = js_block(read("praxis_static/app.js"), "function back() {", "\n}")
        sheet = body.index("model.sheetOpen")
        full = body.index("model.memoryFullscreen")
        history = body.index("model.history.pop()")
        self.assertLess(sheet, full)
        self.assertLess(full, history)

    def test_telegram_back_button_stays_visible_while_the_scene_is_open(self):
        js = read("praxis_static/app.js")
        body = js_block(js, "function updateBack() {", "\n}")
        self.assertIn("model.memoryFullscreen", body)

    def test_leaving_the_section_collapses_the_scene_before_removing_the_feed(self):
        js = read("praxis_static/memory_views.js")
        body = js_block(js, "    destroy() {", "\n    }")
        collapse = body.index("exitFullscreen()")
        remove = body.index("root.remove()")
        self.assertLess(collapse, remove, "снести ленту раньше, чем вернуть сцену, нельзя")

    def test_missing_host_hides_the_button_instead_of_leaving_it_dead(self):
        js = read("praxis_static/memory_views.js")
        self.assertIn("constellation.expand.hidden = !host;", js)
        self.assertIn("codeSpace.expand.hidden = !host;", js)


class TestSceneModulesReachTheBrowser(unittest.TestCase):
    """Отпечаток сборки обязан покрывать модули сцен.

    ⚠ memory_views.js и space3d.js грузятся из app.js БЕЗ `?v=`, а sw.js отдаёт
    /app/static/* cache-first. Пока их байты не участвовали в имени shell-кэша,
    правка сцены не меняла имя — владелец получал новый CSS поверх старого JS.
    Проверяется сам кортеж в _asset_stamp, а не наличие строки где-то в файле.
    """

    def test_asset_stamp_hashes_the_scene_modules(self):
        tree = ast.parse(read("mailroom_bot.py"))
        fn = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == "_asset_stamp")
        names = {const.value
                 for tup in ast.walk(fn) if isinstance(tup, ast.Tuple)
                 for const in tup.elts
                 if isinstance(const, ast.Constant) and isinstance(const.value, str)}
        self.assertLessEqual({"app.js", "app.css", "ambient.js",
                              "memory_views.js", "space3d.js"}, names)


if __name__ == "__main__":
    unittest.main()
