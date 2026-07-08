import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../util/format.dart';
import '../widgets/cards.dart';

/// Control plane — live state plus the write actions. Every write hits an
/// owner-gated, audited ops endpoint; the engine is the source of truth, so
/// after each action we re-read `/control/state`. Destructive actions (engage
/// kill switch, switch to LIVE) require an explicit confirm.
class ControlScreen extends StatefulWidget {
  const ControlScreen({super.key, required this.api, required this.onUnauthorized});
  final ApiClient api;
  final VoidCallback onUnauthorized;

  @override
  State<ControlScreen> createState() => _ControlScreenState();
}

class _ControlScreenState extends State<ControlScreen> {
  Map _state = const {};
  bool _loading = true;
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final data = await widget.api.get('/control/state');
      setState(() {
        _state = data is Map ? data : const {};
        _loading = false;
      });
    } on UnauthorizedException {
      widget.onUnauthorized();
    } catch (e) {
      setState(() {
        _error = e.toString();
        _loading = false;
      });
    }
  }

  /// Run a control POST, surface the result, and re-read state from the engine.
  Future<void> _action(String path, Map<String, dynamic> body, String label) async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      final res = await widget.api.post(path, body: body);
      final ok = res is Map && res['ok'] == true;
      final detail = res is Map ? res['detail'] : null;
      _snack(ok ? '$label ✓' : '$label failed${detail != null ? ' — $detail' : ''}', ok);
      await _load();
    } on UnauthorizedException {
      widget.onUnauthorized();
    } catch (e) {
      _snack('$label failed — $e', false);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _snack(String msg, bool ok) {
    if (!mounted) return;
    final scheme = Theme.of(context).colorScheme;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(msg),
      backgroundColor: ok ? null : scheme.errorContainer,
    ));
  }

  Future<bool> _confirm(String title, String message, {bool destructive = false}) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text(title),
        content: Text(message),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(
            style: destructive
                ? FilledButton.styleFrom(backgroundColor: Theme.of(context).colorScheme.error)
                : null,
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Confirm'),
          ),
        ],
      ),
    );
    return ok == true;
  }

  Map _m(dynamic v) => v is Map ? v : const {};

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null) {
      return RefreshIndicator(
        onRefresh: _load,
        child: ListView(children: [
          const SizedBox(height: 120),
          Center(child: Text(_error!)),
          const SizedBox(height: 12),
          Center(child: FilledButton.tonal(onPressed: _load, child: const Text('Retry'))),
        ]),
      );
    }

    final auto = _m(_state['auto_mode']);
    final ks = _m(_state['kill_switch']);
    final glob = _m(_state['auto_trade_global']);
    final expiry = _m(_state['signal_expiry']);
    final tunables = _m(_state['tunables']);

    final mode = asText(firstOf(auto, ['mode', 'value'])).toLowerCase();
    final ksEngaged = firstOf(ks, ['engaged', 'active', 'tripped']) == true;
    final globalOn = firstOf(glob, ['enabled', 'on']) == true;
    final expiryOn = firstOf(expiry, ['enabled', 'value']) == true;

    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.symmetric(vertical: 8),
        children: [
          if (_busy) const LinearProgressIndicator(),
          _KillSwitchCard(engaged: ksEngaged, busy: _busy, onToggle: _onKillSwitch),
          _autoModeCard(mode),
          Card(
            margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
            child: Column(
              children: [
                SwitchListTile(
                  title: const Text('Global auto-trade'),
                  subtitle: const Text('New orders allowed engine-wide (open positions untouched)'),
                  value: globalOn,
                  onChanged: _busy
                      ? null
                      : (v) => _action('/control/auto-trade-global', {'enabled': v},
                          'Global auto-trade ${v ? 'on' : 'off'}'),
                ),
                const Divider(height: 1),
                SwitchListTile(
                  title: const Text('Signal expiry backstop'),
                  subtitle: const Text('Force-close on max-hold (off = run to TP/SL only)'),
                  value: expiryOn,
                  onChanged: _busy
                      ? null
                      : (v) => _action('/control/signal-expiry', {'enabled': v},
                          'Signal expiry ${v ? 'on' : 'off'}'),
                ),
              ],
            ),
          ),
          _TunablesCard(tunables: tunables, busy: _busy, onEdit: _onEditTunable),
        ],
      ),
    );
  }

  Widget _autoModeCard(String mode) {
    const modes = ['off', 'paper', 'live'];
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Auto-execution mode', style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 12),
            SegmentedButton<String>(
              segments: const [
                ButtonSegment(value: 'off', label: Text('Off')),
                ButtonSegment(value: 'paper', label: Text('Paper')),
                ButtonSegment(value: 'live', label: Text('Live')),
              ],
              selected: {modes.contains(mode) ? mode : 'off'},
              onSelectionChanged: _busy ? null : (sel) => _onAutoMode(sel.first),
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _onAutoMode(String mode) async {
    if (mode == 'live') {
      final ok = await _confirm(
        'Switch to LIVE?',
        'LIVE mode places real orders with real capital. Confirm you want auto-execution LIVE.',
        destructive: true,
      );
      if (!ok) return;
    }
    await _action('/control/auto-mode', {'mode': mode}, 'Auto-mode → ${mode.toUpperCase()}');
  }

  Future<void> _onKillSwitch(bool engage) async {
    if (engage) {
      final ok = await _confirm(
        'Engage kill switch?',
        'This halts ALL auto-trade immediately, engine-wide. Open positions keep their stops but no new orders are placed.',
        destructive: true,
      );
      if (!ok) return;
    }
    await _action('/control/kill-switch', {'engaged': engage, 'reason': 'ops-app'},
        engage ? 'Kill switch ENGAGED' : 'Kill switch disengaged');
  }

  Future<void> _onEditTunable(String key, String label, String current) async {
    final controller = TextEditingController(text: current);
    final result = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text('Edit $label'),
        content: TextField(
          controller: controller,
          autofocus: true,
          keyboardType: const TextInputType.numberWithOptions(decimal: true, signed: true),
          decoration: const InputDecoration(border: OutlineInputBorder()),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, controller.text.trim()), child: const Text('Save')),
        ],
      ),
    );
    if (result == null || result.isEmpty || result == current) return;
    // Send a number when it parses; the engine validates type/range and
    // rejects anything invalid — we surface that error rather than guess.
    final Object value = num.tryParse(result) ?? result;
    await _action('/control/tunables', {
      'values': {key: value},
    }, 'Set $label');
  }
}

class _KillSwitchCard extends StatelessWidget {
  const _KillSwitchCard({required this.engaged, required this.busy, required this.onToggle});
  final bool engaged;
  final bool busy;
  final Future<void> Function(bool engage) onToggle;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      color: engaged ? scheme.errorContainer : null,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          children: [
            Icon(engaged ? Icons.gpp_bad : Icons.gpp_good,
                color: engaged ? scheme.error : scheme.primary, size: 32),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Kill switch', style: Theme.of(context).textTheme.titleMedium),
                  Text(engaged ? 'ENGAGED — all auto-trade halted' : 'Clear',
                      style: TextStyle(color: engaged ? scheme.onErrorContainer : scheme.outline)),
                ],
              ),
            ),
            engaged
                ? FilledButton.tonal(
                    onPressed: busy ? null : () => onToggle(false),
                    child: const Text('Disengage'),
                  )
                : FilledButton(
                    style: FilledButton.styleFrom(backgroundColor: scheme.error),
                    onPressed: busy ? null : () => onToggle(true),
                    child: const Text('Engage'),
                  ),
          ],
        ),
      ),
    );
  }
}

class _TunablesCard extends StatelessWidget {
  const _TunablesCard({required this.tunables, required this.busy, required this.onEdit});
  final Map tunables;
  final bool busy;
  final void Function(String key, String label, String current) onEdit;

  @override
  Widget build(BuildContext context) {
    final list = tunables['tunables'];
    if (list is! List || list.isEmpty) {
      return JsonCard(tunables, title: 'Tunables');
    }
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      child: Padding(
        padding: const EdgeInsets.all(8),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(8, 8, 8, 4),
              child: Text('Tunables', style: Theme.of(context).textTheme.titleMedium),
            ),
            for (final t in list.whereType<Map>())
              ListTile(
                dense: true,
                title: Text(asText(firstOf(t, ['label', 'key', 'name']))),
                subtitle: firstOf(t, ['description']) != null
                    ? Text(asText(firstOf(t, ['description'])), maxLines: 2, overflow: TextOverflow.ellipsis)
                    : null,
                trailing: Text(
                  '${asText(firstOf(t, ['value', 'current']))}${_unit(t)}',
                  style: const TextStyle(fontWeight: FontWeight.w600),
                ),
                onTap: busy
                    ? null
                    : () => onEdit(
                          asText(firstOf(t, ['key', 'name', 'id'])),
                          asText(firstOf(t, ['label', 'key', 'name'])),
                          asText(firstOf(t, ['value', 'current'])),
                        ),
              ),
          ],
        ),
      ),
    );
  }

  String _unit(Map t) {
    final u = firstOf(t, ['unit']);
    return u == null || u == '' ? '' : ' $u';
  }
}
