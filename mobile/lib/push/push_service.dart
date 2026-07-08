import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';

import '../auth/auth_service.dart';

/// Registers this device's FCM token with ops so the monitoring agent can push
/// alerts (Phase 4). Safe to construct even when Firebase failed to initialize
/// — [start] no-ops unless [available] is true.
class PushService {
  PushService(this.auth, {required this.available});

  final AuthService auth;

  /// True only when Firebase initialized (google-services.json was wired at
  /// build time). When false the whole service is inert.
  final bool available;

  String? _token;

  Future<void> start() async {
    if (!available) return;
    try {
      final messaging = FirebaseMessaging.instance;
      await messaging.requestPermission(alert: true, badge: true, sound: true);
      _token = await messaging.getToken();
      if (_token != null) await _register(_token!);
      // Token can rotate; keep ops in sync.
      messaging.onTokenRefresh.listen((t) {
        _token = t;
        _register(t);
      });
    } catch (e) {
      debugPrint('PushService.start failed: $e');
    }
  }

  Future<void> _register(String token) async {
    try {
      await auth.client().post('/devices', body: {
        'fcm_token': token,
        'platform': 'android',
      });
    } catch (e) {
      debugPrint('device register failed: $e');
    }
  }

  /// Best-effort unregister on logout so a signed-out device stops receiving
  /// owner alerts.
  Future<void> unregister() async {
    final token = _token;
    if (!available || token == null) return;
    try {
      await auth.client().delete('/devices', body: {'fcm_token': token});
    } catch (e) {
      debugPrint('device unregister failed: $e');
    }
  }
}
