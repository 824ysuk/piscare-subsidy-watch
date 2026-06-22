"""collect.py の 3 層 deterministic filter の unit test。

直近の Slack 投稿実例（2026-06-17〜2026-06-20）9 件を fixture 化して
各 Layer が期待通り deny / allow することを確認する。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# scripts/ を import path に追加
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from collect import (  # noqa: E402
    classify_admin_prefix,
    classify_bare_municipality_prefix,
    is_industry_excluded,
    is_jgrants_target,
    is_jnet21_target,
    is_title_target,
    is_type_excluded,
)


def _item(title: str) -> dict[str, str]:
    return {"url": "https://example.com/", "title": title, "source": "j-net21"}


def _jgrants_item(title: str, area: str = "福岡県") -> dict[str, str]:
    return {
        "url": "https://example.com/",
        "title": title,
        "source": "jgrants",
        "area": area,
    }


class TestClassifyAdminPrefix(unittest.TestCase):
    """Layer 1: 行政 prefix 解析の単体テスト。"""

    def test_no_prefix_passes(self) -> None:
        allow, reason = classify_admin_prefix("令和8年度 業務改善助成金")
        self.assertTrue(allow)
        self.assertEqual(reason, "no-prefix")

    def test_fukuoka_prefecture_allowed(self) -> None:
        allow, _ = classify_admin_prefix("【福岡県】補助金・助成金：〜")
        self.assertTrue(allow)

    def test_kurume_municipality_allowed(self) -> None:
        allow, _ = classify_admin_prefix("【久留米市】〇〇補助金")
        self.assertTrue(allow)

    def test_kurume_composite_allowed(self) -> None:
        allow, _ = classify_admin_prefix("【福岡県久留米市】〇〇補助金")
        self.assertTrue(allow)

    def test_other_prefecture_denied(self) -> None:
        for prefix in ("【熊本県】", "【千葉県】", "【東京都】", "【大阪府】", "【北海道】"):
            allow, reason = classify_admin_prefix(f"{prefix}〇〇補助金")
            self.assertFalse(allow, msg=f"prefix={prefix} should be denied")
            self.assertTrue(reason.startswith("other-prefecture:"))

    def test_other_fukuoka_municipality_denied(self) -> None:
        for prefix in ("【福岡市】", "【宗像市】", "【北九州市】", "【糸島市】"):
            allow, reason = classify_admin_prefix(f"{prefix}〇〇補助金")
            self.assertFalse(allow, msg=f"prefix={prefix} should be denied")
            self.assertTrue(reason.startswith("other-municipality:"))

    def test_fukuoka_other_municipality_composite_denied(self) -> None:
        allow, reason = classify_admin_prefix("【福岡県宗像市】〇〇補助金")
        self.assertFalse(allow)
        self.assertEqual(reason, "other-municipality:福岡県宗像市")


class TestIsTypeExcluded(unittest.TestCase):
    """Layer 2: type prefix の除外判定の単体テスト。"""

    def test_seminar_excluded(self) -> None:
        self.assertTrue(is_type_excluded("セミナー・イベント：〜"))

    def test_seminar_with_admin_prefix_excluded(self) -> None:
        self.assertTrue(is_type_excluded("【福岡県】セミナー・イベント：脱炭素経営"))

    def test_expert_call_excluded(self) -> None:
        self.assertTrue(is_type_excluded("専門家向け公募：よろず支援拠点"))

    def test_event_exhibitor_excluded(self) -> None:
        self.assertTrue(is_type_excluded("【福岡市】イベント出展者募集：BEYOND SDGs"))

    def test_subsidy_keyword_kept(self) -> None:
        # 「補助金・助成金：〜」は補助金本体なので除外しない
        self.assertFalse(is_type_excluded("【福岡県】補助金・助成金：令和8年度〇〇"))

    def test_no_colon_kept(self) -> None:
        self.assertFalse(is_type_excluded("令和8年度 久留米市WLB 補助金"))


class TestIsIndustryExcluded(unittest.TestCase):
    """Layer 3: 業種固有 keyword の除外判定の単体テスト。"""

    def test_agriculture_excluded(self) -> None:
        self.assertTrue(is_industry_excluded("【千葉県】肥料価格高騰緊急支援事業（農業者向け）"))

    def test_fishery_excluded(self) -> None:
        self.assertTrue(is_industry_excluded("【網走市】水産業パワーアップ事業補助金"))

    def test_forestry_excluded(self) -> None:
        self.assertTrue(is_industry_excluded("令和8年度「A-wood」林業需要拡大事業補助金"))

    def test_nursing_kept(self) -> None:
        self.assertFalse(is_industry_excluded("【福岡県】訪問看護事業所処遇改善補助金"))


class TestIsJnet21TargetActualSlackNoise(unittest.TestCase):
    """直近 Slack 投稿実例 9 件（2026-06-17〜2026-06-20）が全件 deny されることを確認する。

    オオカミ少年化の根本原因。これらが排除されることが本 Issue の主目的。
    """

    NOISE_CASES = [
        # 2026-06-17 piscare-notify 7 件
        "セミナー・イベント：「福岡県海外駐在員とのネットワーキングセミナー in 北九州 2026」",
        "セミナー・イベント：「福岡県海外駐在員とのネットワーキングセミナー in 福岡 2026」",
        "専門家向け公募：「福岡県よろず支援拠点生産性向上支援センター 「生産性向上支援サポーター」公募要領」",
        "【福岡県】セミナー・イベント：「福岡県 脱炭素経営 はじめの一歩。応援セミナー」",
        "【福岡県宗像市】令和8年度 宗像市がんばる中小企業者応援補助金",
        "【福岡県宗像市】令和8年度 宗像市創業応援補助金（“宗業”者応援補助金）",
        "【福岡県宗像市】令和8年度 食のまち宗像推進補助金",
        # 2026-06-18 piscare-notify 1 件
        "【福岡市】イベント出展者募集：「BEYOND SDGs エコプロ」共同出展企業の募集について",
        # 2026-06-20 piscare-notify 1 件
        "【福岡市】補助金・助成金 ：「中小企業奨学金返還支援事業（補助金）」",
    ]

    def test_all_noise_cases_excluded(self) -> None:
        for title in self.NOISE_CASES:
            with self.subTest(title=title):
                self.assertFalse(
                    is_jnet21_target(_item(title)),
                    msg=f"noise が allow されている: {title!r}",
                )


class TestIsJnet21TargetSignalCases(unittest.TestCase):
    """ピスケアにとって signal となるべきケースが allow されることを確認する。"""

    SIGNAL_CASES = [
        # 久留米市の制度
        "【久留米市】令和8年度〇〇補助金",
        "【福岡県久留米市】〇〇補助金",
        # 福岡県の県政（県内事業者対象）
        "【福岡県】補助金・助成金：令和8年度介護DX補助金",
        # 国制度（prefix なし）
        "令和8年度 業務改善助成金",
        "デジタル化・AI導入補助金2026 通常枠 2 次締切",
        "事業承継・M&A補助金（15次公募）",
    ]

    def test_all_signal_cases_allowed(self) -> None:
        for title in self.SIGNAL_CASES:
            with self.subTest(title=title):
                self.assertTrue(
                    is_jnet21_target(_item(title)),
                    msg=f"signal が deny されている: {title!r}",
                )


class TestClassifyBareMunicipalityPrefix(unittest.TestCase):
    """Layer 1b: 【】なし市町村 prefix の単体テスト。"""

    def test_no_prefix_passes(self) -> None:
        allow, reason = classify_bare_municipality_prefix("令和8年度 国制度補助金")
        self.assertTrue(allow)
        self.assertEqual(reason, "no-bare-municipality")

    def test_kurume_municipality_allowed(self) -> None:
        allow, _ = classify_bare_municipality_prefix("久留米市〇〇補助金")
        self.assertTrue(allow)

    def test_kurume_composite_allowed(self) -> None:
        allow, _ = classify_bare_municipality_prefix("福岡県久留米市〇〇補助金")
        self.assertTrue(allow)

    def test_koga_municipality_denied(self) -> None:
        """jGrants で実観測された「古賀市温室効果ガス〜」パターン。"""
        allow, reason = classify_bare_municipality_prefix(
            "古賀市温室効果ガス排出量可視化システム導入費補助金"
        )
        self.assertFalse(allow)
        self.assertEqual(reason, "other-bare-municipality:古賀市")

    def test_fukuoka_city_denied(self) -> None:
        allow, _ = classify_bare_municipality_prefix("福岡市中小企業奨学金返還支援事業")
        self.assertFalse(allow)

    def test_admin_prefix_skipped(self) -> None:
        # 【】で始まる場合は Layer 1b の対象外（Layer 1 で判定済）
        allow, reason = classify_bare_municipality_prefix("【福岡市】〇〇補助金")
        self.assertTrue(allow)
        self.assertEqual(reason, "no-bare-municipality")


class TestIsJgrantsTargetActualNoise(unittest.TestCase):
    """jGrants で実観測された noise が deny されることを確認する。

    dry-run 出力（2026-06-23）で 6 件の noise が観測された:
      - 【福岡県宗像市】3 件（Layer 1）
      - 古賀市〇〇 2 件（Layer 1b）
      - 【福岡市】1 件（Layer 1）
    """

    NOISE_CASES = [
        "【福岡県宗像市】令和8年度 宗像市がんばる中小企業者応援補助金",
        "【福岡県宗像市】令和8年度 宗像市創業応援補助金（“宗業”者応援補助金）",
        "【福岡県宗像市】令和8年度 食のまち宗像推進補助金",
        "古賀市温室効果ガス排出量可視化システム導入費補助金",
        "古賀市中小企業等向け太陽光発電設備導入補助金",
        "【福岡市】グリーンビル促進事業（都心部のオフィスビルなどへの緑化助成）",
    ]

    def test_all_noise_cases_excluded(self) -> None:
        for title in self.NOISE_CASES:
            with self.subTest(title=title):
                self.assertFalse(
                    is_jgrants_target(_jgrants_item(title)),
                    msg=f"jGrants noise が allow されている: {title!r}",
                )


class TestIsJgrantsTargetSignalCases(unittest.TestCase):
    """jGrants で ピスケアにとって signal となるべきケースが allow されることを確認する。"""

    SIGNAL_CASES = [
        # 訪問看護に直接関連する国制度（実観測）
        ("在宅人工呼吸器使用難病患者非常用電源設備整備事業補助金", "全国"),
        # 久留米市の制度（bare municipality 経由）
        ("久留米市WLB 補助金", "福岡県"),
        # 福岡県の県政
        ("【福岡県】補助金・助成金：令和8年度介護DX補助金", "福岡県"),
    ]

    def test_all_signal_cases_allowed(self) -> None:
        for title, area in self.SIGNAL_CASES:
            with self.subTest(title=title):
                self.assertTrue(
                    is_jgrants_target(_jgrants_item(title, area)),
                    msg=f"jGrants signal が deny されている: {title!r}",
                )

    def test_area_outside_fukuoka_denied(self) -> None:
        # area が福岡/全国に含まれない場合は即 deny
        self.assertFalse(
            is_jgrants_target(_jgrants_item("〇〇補助金", area="北海道"))
        )


class TestIsJnet21TargetEastKurumeDefense(unittest.TestCase):
    """東久留米市（東京都）の誤ヒット防御テスト（旧 REGION_EXCLUDE 相当）。"""

    def test_east_kurume_with_prefix_denied(self) -> None:
        self.assertFalse(is_jnet21_target(_item("【東久留米市】〇〇補助金")))

    def test_east_kurume_composite_denied(self) -> None:
        self.assertFalse(is_jnet21_target(_item("【東京都東久留米市】〇〇補助金")))

    def test_east_kurume_in_title_body_denied(self) -> None:
        # prefix なしでも本文に東久留米があれば TITLE_DENY_SUBSTRING で除外
        self.assertFalse(is_jnet21_target(_item("東久留米地区連携セミナー助成金")))


if __name__ == "__main__":
    unittest.main()
