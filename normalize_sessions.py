#!/usr/bin/env python3
"""Reproducible, lossless first-pass audit; never invent missing observations."""
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'sessions_rows.csv'
OUT = ROOT / 'processed'
UTC = timezone.utc
# Explicit project analysis assumption; preserve UTC alongside local time.
LOCAL = timezone(timedelta(hours=5))


def write_csv(name, rows, fields):
    with (OUT / name).open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def timestamp(value, earliest, latest):
    try:
        dt = datetime.fromtimestamp(int(value), UTC)
        return dt if earliest <= dt <= latest else None
    except (ValueError, OverflowError, OSError):
        return None


def dates(start, end):
    while start <= end:
        yield start
        start += timedelta(days=1)


def main():
    source_bytes = SOURCE.read_bytes()
    with SOURCE.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        original_fields = reader.fieldnames
        raw = list(reader)
    assert raw and len({r['id'] for r in raw}) == len(raw), 'Empty input or duplicate IDs'
    created = [datetime.fromisoformat(r['created_at']).astimezone(UTC) for r in raw]
    # Broad validity check, not a reconstruction of device clocks.
    earliest = datetime(min(d.year for d in created), 1, 1, tzinfo=UTC)
    latest = max(created) + timedelta(days=1)
    normalized, flag_counts = [], Counter()
    event_days, received_days = defaultdict(list), Counter()
    device_days = defaultdict(Counter)
    duplicate_keys = {}
    reasons = {'0': 'cow_left', '1': 'bucket_change'}
    for r, received in zip(raw, created):
        flags = []
        start = timestamp(r['start_time'], earliest, latest)
        end = timestamp(r['end_time'], earliest, latest)
        duration = None
        if start is None:
            flags.append('invalid_start_clock')
        if end is None:
            flags.append('invalid_end_clock')
        if start and end:
            if end < start:
                flags.append('end_before_start')
            else:
                duration = int((end - start).total_seconds())
                if duration == 0:
                    flags.append('zero_duration')
                if duration > 7200:
                    flags.append('duration_over_2h_review')
        tag = r['rfid'].strip().upper()
        if not tag:
            flags.append('missing_rfid')
        elif tag.startswith('TEST'):
            flags.append('test_record')
        elif not re.fullmatch(r'[0-9A-F]{24}', tag):
            flags.append('rfid_format_review')
        if r['esp_id'] == '0':
            flags.append('device_zero_review')
        initial, final = Decimal(r['weight_initial']), Decimal(r['weight_final'])
        delta = final - initial
        if delta < 0:
            flags.append('negative_weight_delta')
        elif delta == 0:
            flags.append('zero_weight_delta')
        if r['end_reason'] == '1':
            flags.append('bucket_change')
        elif r['end_reason'] not in reasons:
            flags.append('unknown_end_reason')
        # Screening threshold, not a biological bound or an inferred unit conversion.
        unit = 'kg_assumed_from_current_firmware'
        if max(abs(initial), abs(final)) > 100:
            flags.append('weight_scale_or_outlier_review')
            unit = 'unresolved'
        logs = None
        if not r['weight_log_t'] or not r['weight_log_g']:
            flags.append('missing_weight_log')
        else:
            try:
                t, g = json.loads(r['weight_log_t']), json.loads(r['weight_log_g'])
                assert isinstance(t, list) and isinstance(g, list) and len(t) == len(g)
                assert all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in t + g)
                assert all(x >= 0 for x in t) and all(a <= b for a, b in zip(t, t[1:]))
                logs = len(g)
                if not g:
                    flags.append('empty_weight_log')
                else:
                    if logs == 1:
                        flags.append('single_point_weight_log')
                    if abs(initial - Decimal(str(g[0])) / 1000) > Decimal('0.15'):
                        flags.append('log_initial_mismatch_review')
                    if abs(final - Decimal(str(g[-1])) / 1000) > Decimal('0.15'):
                        flags.append('log_final_mismatch_review')
                    if duration is not None and t[-1] > duration:
                        flags.append('log_exceeds_duration_review')
            except (ValueError, TypeError, AssertionError):
                flags.append('invalid_weight_log')
        if not r['distance_initial_mm'] and not r['distance_final_mm']:
            flags.append('missing_distance')
        elif r['distance_initial_mm'] == '0' and r['distance_final_mm'] == '0':
            flags.append('zero_distance_review')
        key = tuple(r[k] for k in original_fields if k not in ('id', 'created_at'))
        duplicate_of = duplicate_keys.get(key, '')
        if duplicate_of:
            flags.append('duplicate_payload_review')
        else:
            duplicate_keys[key] = r['id']
        day = start.astimezone(LOCAL).date().isoformat() if start else ''
        received_day = received.astimezone(LOCAL).date().isoformat()
        row = dict(r, rfid_normalized=tag, start_utc=start.isoformat() if start else '',
                   end_utc=end.isoformat() if end else '',
                   start_local=start.astimezone(LOCAL).isoformat() if start else '',
                   end_local=end.astimezone(LOCAL).isoformat() if end else '',
                   event_date_local=day, received_date_local=received_day,
                   duration_s=duration if duration is not None else '',
                   end_reason_label=reasons.get(r['end_reason'], 'unknown'),
                   weight_unit_status=unit, weight_delta_source_units=str(delta),
                   weight_delta_kg_assumed=str(delta) if unit != 'unresolved' else '',
                   log_sample_count=logs if logs is not None else '',
                   duplicate_of_id=duplicate_of, provenance='observed', is_imputed='false',
                   quality_flags=';'.join(flags))
        normalized.append(row)
        flag_counts.update(flags)
        received_days[received_day] += 1
        if day:
            event_days[day].append(row)
            device_days[r['esp_id']][day] += 1

    first, last = min(event_days), max(event_days)
    calendar = [d.isoformat() for d in dates(datetime.fromisoformat(first).date(),
                                            datetime.fromisoformat(last).date())]
    daily = []
    for day in calendar:
        rows = event_days[day]
        daily.append(dict(date_local=day, n_sessions=len(rows), n_received=received_days[day],
                          n_missing_rfid=sum(not r['rfid_normalized'] for r in rows),
                          n_positive_delta=sum(Decimal(r['weight_delta_source_units']) > 0 for r in rows),
                          n_zero_delta=sum(Decimal(r['weight_delta_source_units']) == 0 for r in rows),
                          n_negative_delta=sum(Decimal(r['weight_delta_source_units']) < 0 for r in rows),
                          n_with_weight_log=sum(bool(r['weight_log_g']) for r in rows),
                          active_devices=';'.join(sorted({r['esp_id'] for r in rows})),
                          coverage_status='records_present' if rows else 'no_session_records',
                          outage_cause='unconfirmed' if not rows else '',
                          is_imputed='false'))

    gaps = []
    # Device gaps only inside each device's observed span; no assumed commissioning dates.
    for scope, counts in [('all_devices', {d: len(rs) for d, rs in event_days.items() if rs})] + [
            (f'esp_{device}', counts) for device, counts in sorted(device_days.items())]:
        absent = []
        def flush():
            if absent:
                gaps.append(dict(scope=scope, start_date=absent[0], end_date=absent[-1],
                                 days=len(absent), status='absence_of_records',
                                 cause='unconfirmed', engineer_intervention='unknown',
                                 fill_policy='leave_missing'))
                absent.clear()
        for day in calendar:
            if min(counts) <= day <= max(counts) and not counts.get(day, 0):
                absent.append(day)
            else:
                flush()
        flush()

    OUT.mkdir(exist_ok=True)
    write_csv('sessions_normalized.csv', normalized, list(normalized[0]))
    write_csv('daily_coverage.csv', daily, list(daily[0]))
    write_csv('gaps.csv', gaps, ['scope', 'start_date', 'end_date', 'days', 'status',
                               'cause', 'engineer_intervention', 'fill_policy'])
    metadata = dict(source_file=SOURCE.name, source_sha256=hashlib.sha256(source_bytes).hexdigest(),
                    rows=len(raw), event_date_range=[first, last], local_timezone='UTC+05:00 (assumed)',
                    imputed_rows=0, flags=dict(sorted(flag_counts.items())),
                    parameters=dict(clock_earliest=earliest.isoformat(), clock_latest=latest.isoformat(),
                                    weight_scale_review_threshold=100, duration_review_s=7200,
                                    log_endpoint_review_kg=0.15))
    (OUT / 'audit.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')
    all_gaps = [g for g in gaps if g['scope'] == 'all_devices']
    report = f'''# Первичный аудит выгрузки Milkwarden

Исходник: `{SOURCE.name}`. SHA-256: `{metadata['source_sha256']}`.
Записей: {len(raw)}. Даты корректного начала сессий: {first} — {last}.
Время для календаря: UTC+05:00 (явное допущение анализа). Исходные поля сохранены.
Добавленных наблюдений и восстановленных значений: **0**.

## Основные результаты

- Без RFID: {flag_counts['missing_rfid']} ({flag_counts['missing_rfid'] / len(raw):.1%}).
- Некорректное время начала: {flag_counts['invalid_start_clock']}.
- Тестовые записи: {flag_counts['test_record']}.
- Отрицательная разность весов: {flag_counts['negative_weight_delta']}.
- Нулевая разность весов: {flag_counts['zero_weight_delta']}.
- Требуют проверки масштаба веса или выброса: {flag_counts['weight_scale_or_outlier_review']}.
- Без весового журнала: {flag_counts['missing_weight_log']}.
- Код смены бидона: {flag_counts['bucket_change']}.

Периоды без записей с корректной датой начала сессии:
'''
    report += '\n'.join(f"- {g['start_date']} — {g['end_date']}: {g['days']} дней." for g in all_gaps)
    report += '''

## Интерпретация и ограничения

Отсутствие записей не доказывает отказ оборудования или выезд инженера: возможны
простой, особенности выгрузки, потеря связи и другие причины. Все интервалы в
`gaps.csv` оставлены незаполненными. Крайние дни выгрузки могут быть неполными.
Дни до первого появления устройства и после последнего не объявляются его сбоями.
Календарь считает сессии, включая диагностические; это не число доений или коров.
Записи с неверным временем остаются в основной таблице, но не в календаре сессий;
`n_received` отдельно показывает даты поступления на сервер. Эти даты не подменяют
даты измерений. Ноль сессий не означает нулевой надой.

Пустых `weight_initial` и `weight_final` нет. Восстанавливать здесь приходится
потенциально отсутствующие сессии либо повреждённые измерения, а не пустые веса.
Без списка животных и расписания нельзя определить число отсутствующих доений.
RFID не угадывается по соседним строкам; число уникальных меток не равно числу коров.

Текущая прошивка `integrated/esp32/esp32_milkwarden/src/modules/cloud/cloud.cpp`
перед отправкой делит веса на 1000: предполагаемая единица полей веса — кг,
а `weight_log_g` — граммы. История прошивок в выгрузке не указана: допущение
не подтверждает единицы каждой исторической записи. Значения по модулю больше
100 помечены для проверки, а не автоматически пересчитаны. Порог 100 — только
эвристика контроля качества, не физиологический норматив. Разность весов
сохранена, но не названа надоем; отрицательные значения не заменены нулём.
Расхождение краёв весового журнала с итоговыми весами больше 0,15 кг — флаг
проверки, не доказательство ошибки: последний отсчёт может предшествовать концу.

В `src/config.h` прошивки `end_reason=0` означает уход коровы, `1` — смену бидона.
В `server/data/kellerovka/index.html` те же коды отображаются как «успешно»/«ошибка».
Для анализа использована трактовка прошивки; она также требует сверки с версиями.
Сессия со сменой бидона может быть частью одного доения.

Все непустые дистанции в этой выгрузке равны нулю. Они сохранены и помечены:
по ним нельзя обосновать измеренный уровень молока без проверки датчика.

## Следующий этап для статьи

1. Сверить начало штатной эксплуатации, журнал обслуживания, версии прошивки,
   единицы измерения, перечень RFID и расписание доений.
2. Утвердить интервалы отказов и оставить их пропущенными во всех оценках.
3. Для отдельных коротких пропусков при известной корове и периоде доения
   проверить метод восстановления на временно скрытых реальных наблюдениях.
   Показать ошибку восстановления и неопределённость; не переносить результат
   через смену режима работы или длительный перерыв.
4. Хранить оценки отдельно с методом, исходными опорными наблюдениями и признаком
   `is_imputed=true`; сравнить выводы статьи с оценками и без них. Синтетический
   набор для демонстрации также хранить отдельно и обозначать как симуляцию.
5. Присоединять погоду по явной дате/времени, координатам и источнику, не заменяя
   измерения молока погодными предположениями.

Уже доступные темы статьи: полнота идентификации RFID, непрерывность регистрации,
качество синхронизации времени, особенности смены бидона и согласованность
весового журнала с итоговой записью. Точность измерения молока потребует эталонных
контрольных взвешиваний, а подтверждение ремонтных простоев — журнала обслуживания.
'''
    (OUT / 'audit_ru.md').write_text(report, encoding='utf-8')
    assert SOURCE.read_bytes() == source_bytes, 'Source was modified'
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
