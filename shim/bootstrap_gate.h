#ifndef BE5_BOOTSTRAP_GATE_H
#define BE5_BOOTSTRAP_GATE_H

typedef struct {
    int preload_attempted;
    int preload_succeeded;
    int chain_attempted;
    int chain_succeeded;
    int in_boot;
} be5_bootstrap_gate;

static int be5_begin_preload(be5_bootstrap_gate *gate) {
    if (gate->preload_attempted || gate->in_boot) return 0;
    gate->preload_attempted = 1;
    gate->in_boot = 1;
    return 1;
}

static void be5_finish_preload(be5_bootstrap_gate *gate, int result) {
    gate->preload_succeeded = result == 0;
    gate->in_boot = 0;
}

static int be5_begin_chain(be5_bootstrap_gate *gate, int trigger_matches) {
    if (gate->chain_attempted || !gate->preload_succeeded || gate->in_boot ||
        !trigger_matches)
        return 0;
    gate->chain_attempted = 1;
    gate->in_boot = 1;
    return 1;
}

static void be5_finish_chain(be5_bootstrap_gate *gate, int result) {
    gate->chain_succeeded = result == 0;
    gate->in_boot = 0;
}

#endif
