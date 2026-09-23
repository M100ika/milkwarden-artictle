#!/usr/bin/env python3
"""Generate explicitly fictional milking sessions; never modify observations."""
import csv
import hashlib
import json
import math
import random
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEED = 20260921
START, END = date(2026, 5, 17), date(2026, 9, 21)
GAP_START, GAP_END = date(2026, 6, 24), date(2026, 7, 10)
LOCAL = timezone(timedelta(hours=5))


def main():
    rng = random.Random(SEED)
    source = ROOT / 'sessions_rows.csv'
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    with source.open(encoding='utf-8-sig', newline='') as f:
        source_fields = next(csv.reader(f))
    fields = source_fields + ['cow_id', 'milking_period', 'local_date',
                              'milk_mass_kg', 'provenance', 'is_synthetic', 'scenario_id']
    # All model parameters below are invented, not fitted estimates of this herd.
    cows = [{
        'id': f'SYN_COW_{i + 1:02d}',
        'daily_kg': rng.uniform(10.5, 18.5),
        'decline': rng.uniform(0.0005, 0.0020),
        'morning_share': rng.uniform(0.53, 0.58),
        'state': 0.0,
    } for i in range(16)]
    rows, coverage = [], []
    common_state = 0.0
    day = START
    while day <= END:
        elapsed = (day - START).days
        common_state = 0.65 * common_state + rng.gauss(0, 0.025)
        # Evolve latent yield even through the simulated recording outage.
        yields = {}
        for cow in cows:
            cow['state'] = 0.55 * cow['state'] + rng.gauss(0, 0.04)
            yields[cow['id']] = (cow['daily_kg'] * math.exp(-cow['decline'] * elapsed)
                                 * math.exp(common_state + cow['state']))
        if GAP_START <= day <= GAP_END:
            coverage.append({'local_date': day.isoformat(), 'expected_sessions': 32,
                             'generated_sessions': 0, 'missing_sessions': 32,
                             'scenario_status': 'simulated_outage', 'is_synthetic': 'true'})
            day += timedelta(days=1)
            continue
        count = 0
        for period, hour in [('morning', 6), ('evening', 18)]:
            order = list(cows)
            rng.shuffle(order)
            shift_start = datetime.combine(day, time(hour), LOCAL) + timedelta(minutes=rng.randint(-15, 15))
            next_start = [shift_start + timedelta(seconds=rng.randint(0, 90)) for _ in range(4)]
            for index, cow in enumerate(order):
                device = index % 4
                share = cow['morning_share'] if period == 'morning' else 1 - cow['morning_share']
                milk = round(yields[cow['id']] * share * math.exp(rng.gauss(0, 0.035)), 1)
                duration = int(max(240, min(900, 180 + milk * 43 + rng.gauss(0, 45))))
                start = next_start[device]
                end = start + timedelta(seconds=duration)
                next_start[device] = end + timedelta(seconds=rng.randint(45, 150))
                # An omitted record is a recording loss, not zero production.
                if rng.random() < 0.025:
                    continue
                initial = round(rng.uniform(0.3, 1.3), 1)
                final = round(initial + milk, 1)
                log_t = list(range(0, duration, 20)) + [duration]
                log_g = []
                for sec in log_t:
                    fraction = sec / duration
                    progress = 3 * fraction ** 2 - 2 * fraction ** 3
                    noise = rng.gauss(0, 12) if 0 < sec < duration else 0
                    log_g.append(round(1000 * (initial + milk * progress) + noise))
                received = end + timedelta(seconds=rng.randint(1, 18))
                rows.append(dict(
                    id='', esp_id=device + 1, rfid=cow['id'],
                    weight_initial=f'{initial:.1f}', weight_final=f'{final:.1f}',
                    start_time=int(start.timestamp()), end_time=int(end.timestamp()),
                    end_reason=0, created_at=received.astimezone(timezone.utc).isoformat(),
                    distance_initial_mm='', distance_final_mm='',
                    weight_log_t=json.dumps(log_t, separators=(',', ':')),
                    weight_log_g=json.dumps(log_g, separators=(',', ':')),
                    cow_id=cow['id'], milking_period=period, local_date=day.isoformat(),
                    milk_mass_kg=f'{milk:.1f}', provenance='synthetic_simulation',
                    is_synthetic='true', scenario_id='fictional_16_cows_2026_v1'))
                count += 1
        coverage.append({'local_date': day.isoformat(), 'expected_sessions': 32,
                         'generated_sessions': count, 'missing_sessions': 32 - count,
                         'scenario_status': 'simulated_partial_loss' if count < 32 else 'simulated_recording',
                         'is_synthetic': 'true'})
        day += timedelta(days=1)
    rows.sort(key=lambda r: (r['start_time'], r['esp_id']))
    for i, row in enumerate(rows, 1):
        row['id'] = f'SYN_{i:06d}'
    for name, data, columns in [
        ('sessions_synthetic.csv', rows, fields),
        ('synthetic_daily_coverage.csv', coverage, list(coverage[0])),
    ]:
        with (ROOT / name).open('w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(data)
    metadata = {
        'provenance': 'synthetic_simulation', 'seed': SEED, 'rows': len(rows),
        'date_range': [START.isoformat(), END.isoformat()], 'timezone': 'UTC+05:00',
        'cows': 16, 'milkings_per_cow_per_day_assumed': 2, 'devices_assumed': 4,
        'simulated_outage_inclusive': [GAP_START.isoformat(), GAP_END.isoformat()],
        'outage_cause': 'fictional_scenario_not_verified_engineering_history',
        'additional_record_loss_probability': 0.025,
        'units': {'weight_initial': 'kg', 'weight_final': 'kg', 'milk_mass_kg': 'kg',
                  'weight_log_g': 'g', 'weight_log_t': 'seconds_since_start'},
        'initial_daily_yield_range_kg_assumed': [10.5, 18.5],
        'daily_exponential_decline_range_assumed': [0.0005, 0.002],
        'model': 'Invented cow baselines, gradual decline, correlated daily variation, morning/evening split.',
        'warning_ru': 'Полностью выдуманные данные. Не являются измерениями, восстановлением реальных надоев или доказательством результатов эксперимента.',
        'original_source_sha256': original_hash,
        'source_usage': 'Only CSV column names were reused; no observed rows or RFID identities were copied.',
    }
    (ROOT / 'sessions_synthetic.meta.json').write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    print(json.dumps({'rows': len(rows), 'cows': 16, 'calendar_days': len(coverage),
                      'outage_days': sum(c['scenario_status'] == 'simulated_outage' for c in coverage),
                      'additional_missing_sessions': sum(c['missing_sessions'] for c in coverage
                                                         if c['scenario_status'] != 'simulated_outage')}, indent=2))


if __name__ == '__main__':
    main()
