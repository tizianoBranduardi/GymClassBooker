from datetime import datetime

import fmt


def day_label(date_str):
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%a %d %b")

OPTIONS = [
    {"idx": 1, "date": "2026-10-30", "time": "18:15", "name": "Pilates", "studio": "Main",
     "booked": 5, "capacity": 20, "class_id": "c1", "session_id": "s1"},
    {"idx": 2, "date": "2026-10-30", "time": "19:00", "name": "Spin <xl>", "studio": "Cycle",
     "booked": 19, "capacity": 20, "class_id": "c2", "session_id": "s2"},
    {"idx": 3, "date": "2026-10-31", "time": "18:30", "name": "Yoga", "studio": "Studio2",
     "booked": 14, "capacity": 20, "class_id": "c3", "session_id": "s3"},
]


def test_options_grouped_by_day_with_headers():
    out = fmt.format_options(OPTIONS)
    d30, d31 = day_label("2026-10-30"), day_label("2026-10-31")
    assert out.index(d30) < out.index("Pilates") < out.index(d31) < out.index("Yoga")
    assert "📅" in out and "/pick" in out


def test_options_numbering_matches_idx():
    out = fmt.format_options(OPTIONS)
    assert "1 ·" in out and "2 ·" in out and "3 ·" in out


def test_options_fullness_icons():
    out = fmt.format_options(OPTIONS)
    assert "🟢 5/20" in out      # 25%
    assert "🔴 19/20" in out     # 95%
    assert "🟡 14/20" in out     # 70%


def test_options_escapes_html_in_names():
    out = fmt.format_options(OPTIONS)
    assert "<xl>" not in out and "&lt;xl&gt;" in out


def test_bookings_list():
    items = [
        {"booking_id": "2026-10-30_1815_c1", "name": "Pilates", "date": "2026-10-30",
         "time": "18:15", "status": "scheduled", "fire_at": "2026-10-28 18:12"},
        {"booking_id": "x", "name": "Spin", "date": "2026-10-20", "time": "19:00",
         "status": "booked"},
    ]
    out = fmt.format_bookings(items)
    assert "📅" in out and "✅" in out
    assert "2026-10-30_1815_c1" in out and "18:12" in out


def test_scheduled_confirmation():
    out = fmt.format_scheduled(OPTIONS[0], "2026-10-28 18:12", "2026-10-30_1815_c1")
    assert "Pilates" in out and "18:12" in out and "/cancel 2026-10-30_1815_c1" in out


def test_immediate_confirmation():
    out = fmt.format_immediate(OPTIONS[0])
    assert "Pilates" in out and "already open" in out.lower()
