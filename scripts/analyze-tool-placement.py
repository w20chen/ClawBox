"""Summarize preserved four-VM placement trials; never execute or change a run.

Usage: python scripts/analyze-tool-placement.py RUN_ROOT [...] --output NEW_DIR
Requires numpy, scipy and matplotlib for the standalone plots and statistics.
"""
import argparse
import json
from pathlib import Path
import statistics


def has_special_case(row):
    return bool(row['special_case_flags'] or row['baseline_exit_code']
                or row.get('network_command_candidate', False))


def analyze(roots, output):
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for root in roots:
        for path in sorted(root.rglob('paired-trials.json')):
            target = json.loads((path.parent/'baseline-target.json').read_text())
            trials = json.loads(path.read_text())
            pairs = {}
            invalid = []
            for item in trials:
                trial_path = path.parent/Path(item['artifact']).parent.name/'trial.json'
                trial = json.loads(trial_path.read_text())
                replicas = trial.get('replicas', [])
                # Different exit outcomes are different workloads, not a placement speedup.
                comparable = (trial['status'] == 'measured' and len(replicas) == 4
                    and all(r['bridge']['exit_code'] == target['baseline_exit_code'] for r in replicas))
                if comparable:
                    pairs.setdefault(item['repeat'], {})[item['layout']] = trial['makespan_seconds']
                else:
                    invalid.append({'repeat': item['repeat'], 'layout': item['layout'],
                                    'status': trial['status'], 'error': trial.get('error'),
                                    'exit_codes': [r.get('bridge', {}).get('exit_code') for r in replicas]})
            for layout in sorted({t['layout'] for t in trials} - {'packed'}):
                valid = [{'repeat': repeat, 'packed_seconds': p['packed'], 'spread_seconds': p[layout],
                          'sensitivity': p['packed']/p[layout]-1,
                          'difference_seconds': p['packed']-p[layout]}
                         for repeat, p in pairs.items() if 'packed' in p and layout in p]
                row = {**target, 'source': str(path), 'comparison': layout, 'pairs': valid,
                       'invalid_trials': invalid, 'paired_repetitions': len(valid),
                       'placement_trial_status': 'measured' if valid else 'insufficient_pairs',
                       'repeat_uncertainty_available': len(valid) >= 2}
                if valid:
                    values = [p['sensitivity'] for p in valid]
                    row.update(sensitivity_median=statistics.median(values),
                               sensitivity_min=min(values), sensitivity_max=max(values),
                               difference_seconds_median=statistics.median(p['difference_seconds'] for p in valid))
                rows.append(row)
    with (output/'placement-results.jsonl').open('w', encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps(row)+'\n')
    import numpy as np
    from scipy.stats import spearmanr, rankdata
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    summaries = {}
    for comparison in sorted({r['comparison'] for r in rows}):
        selected = [r for r in rows if r['comparison'] == comparison and r['placement_trial_status'] == 'measured']
        # Preserve separate runs, but count a target only once in aggregate correlations.
        unique = {r['target_id']: r for r in selected}
        selected = list(unique.values())
        summary = {'measured_unique_targets': len(selected), 'comparison': comparison, 'strata': {}}
        for label, subset in [('all', selected), ('clean_success', [r for r in selected
                              if not has_special_case(r)])]:
            x = np.array([r['llc_hits_per_kinst'] for r in subset], dtype=float)
            y = np.array([r['sensitivity_median'] for r in subset], dtype=float)
            stat = {'targets': len(subset)}
            if len(subset) >= 3 and np.ptp(x) and np.ptp(y):
                rho = float(spearmanr(x, y).statistic)
                # Resample whole traces, keeping correlated tools from each trace together.
                rng = np.random.default_rng(51)
                cases = sorted({r['case_id'] for r in subset})
                indices = {c: [i for i, r in enumerate(subset) if r['case_id'] == c] for c in cases}
                boot = []
                if len(cases) >= 3:
                    for _ in range(1000):
                        take = [i for c in rng.choice(cases, len(cases), replace=True) for i in indices[c]]
                        if np.ptp(x[take]) and np.ptp(y[take]):
                            boot.append(float(spearmanr(x[take], y[take]).statistic))
                stat.update(spearman_rho=rho, trace_cluster_bootstrap_95ci=(np.quantile(boot, [.025,.975]).tolist() if boot else None))
            summary['strata'][label] = stat
        memory_rows = [r for r in selected if r['memory_eligible'] and r.get('memory_extra_peak_bytes') is not None]
        if len(memory_rows) >= 5:
            mx = np.array([r['llc_hits_per_kinst'] for r in memory_rows])
            my = np.array([r['sensitivity_median'] for r in memory_rows])
            mm = np.array([r['memory_extra_peak_bytes'] for r in memory_rows])
            if np.ptp(mx) and np.ptp(my) and np.ptp(mm):
                design = np.column_stack([np.ones(len(mm)), rankdata(mm)])
                residuals = [rankdata(v)-design@np.linalg.lstsq(design,rankdata(v),rcond=None)[0] for v in (mx,my)]
                if all(np.ptp(r) > 1e-12 for r in residuals):
                    summary['partial_spearman_controlling_memory_footprint'] = float(np.corrcoef(*residuals)[0,1])
                summary['memory_footprint_quartiles'] = []
                for indices in np.array_split(np.argsort(mm), 4):
                    summary['memory_footprint_quartiles'].append({
                        'n': len(indices), 'min_bytes': int(mm[indices].min()), 'max_bytes': int(mm[indices].max()),
                        'median_sensitivity': float(np.median(my[indices])),
                        'spearman_rho': float(spearmanr(mx[indices],my[indices]).statistic)
                            if len(indices) >= 3 and np.ptp(mx[indices]) and np.ptp(my[indices]) else None})
        fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), constrained_layout=True)
        for flagged, color, label in [(False, 'tab:blue', 'No special flag'), (True, 'tab:orange', 'Network candidate/environment/exit flag')]:
            subset = [r for r in selected if has_special_case(r) == flagged]
            for r in (r for r in subset if r['repeat_uncertainty_available']):
                x, y = r['llc_hits_per_kinst'], r['sensitivity_median']*100
                axes[0].errorbar(x, y, yerr=[[y-r['sensitivity_min']*100], [r['sensitivity_max']*100-y]],
                                 color=color, alpha=.25, linewidth=.6)
            axes[0].scatter([r['llc_hits_per_kinst'] for r in subset], [r['sensitivity_median']*100 for r in subset],
                            s=14, color=color, alpha=.65, label=label)
        points = axes[1].scatter([r['llc_hits_per_kinst'] for r in memory_rows],
            [r['sensitivity_median']*100 for r in memory_rows], s=16,
            c=[np.log10(1+max(0,r['memory_extra_peak_bytes'])/2**20) for r in memory_rows], cmap='viridis')
        if memory_rows:
            fig.colorbar(points, ax=axes[1], label='log10(1 + extra memory peak / MiB)')
        for ax in axes:
            ax.axhline(0, color='gray', linewidth=.7)
            ax.set_xlabel('LLC hits / kInst (profiling replay)')
            ax.set_ylabel('(packed / spread makespan - 1) × 100%')
            ax.grid(alpha=.15)
        axes[0].legend(fontsize=8)
        axes[0].set_title(f'{comparison}: {len(selected)} tools; single pairs have no error bar')
        axes[1].set_title('Memory footprint is a proxy, not LLC working set')
        fig.savefig(output/f'llc-sensitivity-{comparison}.png', dpi=200)
        fig.savefig(output/f'llc-sensitivity-{comparison}.pdf')
        plt.close(fig)
        summaries[comparison] = summary
    summary = {'comparisons': summaries, 'rows': len(rows),
        'clean_success_definition': 'Exit zero, no special-case evidence flags, and not a network-command candidate. A candidate label does not prove network transfer.',
        'definition': 'sensitivity = packed makespan / spread makespan - 1; positive favors spread',
        'uncertainty': 'Single pairs have no repeat uncertainty. Repeated pairs retain their range. Spearman CI uses trace-cluster bootstrap, not repeated timing.',
        'limits': ['Cross-NUMA spread changes memory locality and bandwidth as well as cache placement.',
                   'Cluster isolation permits shared upper LLC across concurrent groups; inspect each run topology.',
                   'Historical profiling R is observational; same-prestate parent PMU is preserved separately.',
                   'Network/environment and nonzero-exit tools are retained and separately stratified.',
                   'Memory footprint is not a measurement of cache working-set size.']}
    (output/'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', nargs='+', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    analyze(args.runs, args.output)
