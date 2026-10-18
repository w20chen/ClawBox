# LLC placement reverse-order repeat contract

This repeat was frozen while the first comparison was still running, before its
C0 performance result was available.

- Spec: `pilot-c8-real-reverse.yaml`
- Semantic spec digest: `150c8faad46c4646d58a9689dd979b256b7b493b17499f534d84cbf0767584e7`
- Only intentional differences from the first screening spec are the experiment
  ID and random seed `20261019`.
- The seed reverses the arm order from C2/C0 to C0/C2.
- Workload, density, VM shape, resource policy, topology policy, thresholds,
  validation, timeouts, and primary metrics remain unchanged.

The repeat is intended to detect a large order or host-time effect. Two paired
runs remain screening evidence; they do not establish a stable population
speedup or equivalence.
