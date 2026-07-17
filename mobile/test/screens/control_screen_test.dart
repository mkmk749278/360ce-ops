/// Widget tests for the control plane's confirm gates.
///
/// The control doctrine says destructive actions (engage kill switch,
/// switch auto-mode to LIVE) require an explicit confirm, and that after
/// every write the screen re-reads `/control/state` because the engine is
/// the source of truth.  These are exactly the properties a refactor could
/// silently drop, so they get pinned at the widget level:
///
/// * Engage kill switch → confirm dialog; Cancel sends NOTHING.
/// * Confirm → POST /control/kill-switch + a state re-read.
/// * Auto-mode LIVE → confirm dialog; Cancel sends nothing; paper does
///   not ask (non-destructive).
/// * Disengage (making things safer) never asks for confirmation.
/// * 401 anywhere → onUnauthorized fires (bounce to login).
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:ops360/api/api_client.dart';
import 'package:ops360/screens/control_screen.dart';

class _FakeApi extends Fake implements ApiClient {
  _FakeApi({Map<String, dynamic>? state}) : state = state ?? _defaultState;

  static const Map<String, dynamic> _defaultState = {
    'auto_mode': {'mode': 'paper'},
    'kill_switch': {'engaged': false},
    'auto_trade_global': {'enabled': true},
    'signal_expiry': {'enabled': true},
    'tunables': {'tunables': []},
  };

  Map<String, dynamic> state;
  bool throwUnauthorized = false;
  int stateReads = 0;
  final List<(String, Object?)> posts = [];

  @override
  Future<dynamic> get(String path, {Map<String, dynamic>? query}) async {
    if (throwUnauthorized) throw const UnauthorizedException();
    if (path == '/control/state') {
      stateReads++;
      return state;
    }
    return {};
  }

  @override
  Future<dynamic> post(String path, {Object? body}) async {
    posts.add((path, body));
    return {'ok': true};
  }
}

Widget _host(_FakeApi api, {VoidCallback? onUnauthorized}) => MaterialApp(
      home: Scaffold(
        body: ControlScreen(
          api: api,
          onUnauthorized: onUnauthorized ?? () {},
        ),
      ),
    );

void main() {
  testWidgets('renders the control surface from engine state',
      (tester) async {
    final api = _FakeApi();
    await tester.pumpWidget(_host(api));
    await tester.pumpAndSettle();
    expect(find.text('Kill switch'), findsOneWidget);
    expect(find.text('Global auto-trade'), findsOneWidget);
    expect(api.stateReads, 1);
  });

  testWidgets('engage kill switch asks for confirmation; cancel sends nothing',
      (tester) async {
    final api = _FakeApi();
    await tester.pumpWidget(_host(api));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Engage'));
    await tester.pumpAndSettle();
    expect(find.text('Engage kill switch?'), findsOneWidget);

    await tester.tap(find.text('Cancel'));
    await tester.pumpAndSettle();
    expect(api.posts, isEmpty);
    expect(api.stateReads, 1); // no write → no re-read
  });

  testWidgets('confirmed engage posts and re-reads engine state',
      (tester) async {
    final api = _FakeApi();
    await tester.pumpWidget(_host(api));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Engage'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Confirm'));
    await tester.pumpAndSettle();

    expect(api.posts, hasLength(1));
    final (path, body) = api.posts.single;
    expect(path, '/control/kill-switch');
    expect(body, containsPair('engaged', true));
    // The engine is the source of truth: state was re-read after the write.
    expect(api.stateReads, 2);
  });

  testWidgets('disengaging an engaged kill switch does not ask to confirm',
      (tester) async {
    final api = _FakeApi(state: {
      ..._FakeApi._defaultState,
      'kill_switch': {'engaged': true},
    });
    await tester.pumpWidget(_host(api));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Disengage'));
    await tester.pumpAndSettle();
    // Straight to the POST — making things safer never needs a gate.
    expect(api.posts.single.$1, '/control/kill-switch');
    expect(api.posts.single.$2, containsPair('engaged', false));
  });

  testWidgets('switching auto-mode to LIVE requires confirmation',
      (tester) async {
    final api = _FakeApi();
    await tester.pumpWidget(_host(api));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Live'));
    await tester.pumpAndSettle();
    expect(find.text('Switch to LIVE?'), findsOneWidget);

    await tester.tap(find.text('Cancel'));
    await tester.pumpAndSettle();
    expect(api.posts, isEmpty);
  });

  testWidgets('switching auto-mode to OFF posts without a dialog',
      (tester) async {
    final api = _FakeApi();
    await tester.pumpWidget(_host(api));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Off'));
    await tester.pumpAndSettle();
    expect(api.posts.single.$1, '/control/auto-mode');
    expect(api.posts.single.$2, containsPair('mode', 'off'));
  });

  testWidgets('401 on load bounces to login via onUnauthorized',
      (tester) async {
    final api = _FakeApi()..throwUnauthorized = true;
    var bounced = false;
    await tester.pumpWidget(_host(api, onUnauthorized: () => bounced = true));
    // Plain pump: the screen deliberately stays on its spinner after a 401
    // (the host is about to swap to the login screen), so pumpAndSettle
    // would never settle.
    await tester.pump();
    expect(bounced, isTrue);
  });
}
