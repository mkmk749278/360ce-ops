import 'package:dio/dio.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:local_auth/local_auth.dart';

import '../api/api_client.dart';
import '../config.dart';

/// Owns the app's credentials and unlock state:
///
/// * base URL + app-token, persisted in the device keystore-backed secure
///   storage (never plaintext prefs);
/// * the login exchange (ops password → app-token);
/// * the biometric / device-PIN gate enforced on cold start.
///
/// The app never stores or sees the engine's owner-tier Bearer — only the
/// ops-issued app-token.
class AuthService {
  AuthService({FlutterSecureStorage? storage, LocalAuthentication? localAuth})
      : _storage = storage ?? const FlutterSecureStorage(),
        _localAuth = localAuth ?? LocalAuthentication();

  static const _kBaseUrl = 'ops_base_url';
  static const _kToken = 'ops_app_token';
  static const _kBiometric = 'ops_biometric_enabled';

  final FlutterSecureStorage _storage;
  final LocalAuthentication _localAuth;

  String? _baseUrl;
  String? _token;

  String get baseUrl => _baseUrl ?? Config.defaultBaseUrl;
  bool get isLoggedIn => _token != null && _token!.isNotEmpty;

  /// Build an [ApiClient] bound to the current base URL + token.
  ApiClient client() => ApiClient(baseUrl: baseUrl, token: _token);

  /// Load persisted credentials at startup. Returns true if a token exists.
  Future<bool> load() async {
    _baseUrl = await _storage.read(key: _kBaseUrl);
    _token = await _storage.read(key: _kToken);
    return isLoggedIn;
  }

  /// Exchange the ops password for an app-token and persist it. Throws
  /// [ApiException] on wrong password / unreachable server.
  Future<void> login({required String baseUrl, required String password}) async {
    final normalized = baseUrl.trim().replaceAll(RegExp(r'/+$'), '');
    final dio = Dio(BaseOptions(
      baseUrl: '$normalized/api/v1',
      connectTimeout: Config.requestTimeout,
      receiveTimeout: Config.requestTimeout,
      validateStatus: (c) => c != null && c < 500,
    ));
    Response resp;
    try {
      resp = await dio.post('/auth/login', data: {'password': password});
    } on DioException {
      throw const ApiException('Cannot reach ops. Check the server URL.');
    }
    if (resp.statusCode == 401) {
      throw const ApiException('Wrong password.');
    }
    if (resp.statusCode != 200 || resp.data is! Map || resp.data['token'] == null) {
      throw ApiException('Unexpected login response (HTTP ${resp.statusCode}).');
    }
    _baseUrl = normalized;
    _token = resp.data['token'] as String;
    await _storage.write(key: _kBaseUrl, value: _baseUrl);
    await _storage.write(key: _kToken, value: _token);
  }

  /// Clear the local token (does not revoke server-side; use revoke-all for
  /// the lost-phone case).
  Future<void> logout() async {
    _token = null;
    await _storage.delete(key: _kToken);
  }

  /// Server-side revoke of every app-token, then local logout.
  Future<void> revokeAllDevices() async {
    try {
      await client().post('/auth/revoke-all');
    } finally {
      await logout();
    }
  }

  /// Whether the owner has opted into app-lock. Default OFF, so a device with
  /// no enrolled biometric is never forced to authenticate.
  Future<bool> biometricEnabled() async =>
      (await _storage.read(key: _kBiometric)) == '1';

  Future<void> setBiometricEnabled(bool value) =>
      _storage.write(key: _kBiometric, value: value ? '1' : '0');

  /// True only when the device actually has an enrolled biometric to check.
  Future<bool> biometricsAvailable() async {
    try {
      if (!await _localAuth.isDeviceSupported()) return false;
      return (await _localAuth.getAvailableBiometrics()).isNotEmpty;
    } on Exception {
      return false;
    }
  }

  /// Prompt for biometric / device-PIN unlock. Returns true on success, or
  /// true when the device has no enrolled auth (nothing to gate on) so the
  /// app stays usable on a device without biometrics.
  Future<bool> unlock() async {
    try {
      final supported = await _localAuth.isDeviceSupported();
      if (!supported) return true;
      final canCheck = await _localAuth.canCheckBiometrics ||
          (await _localAuth.getAvailableBiometrics()).isNotEmpty;
      if (!canCheck) return true;
      return await _localAuth.authenticate(
        localizedReason: 'Unlock ${Config.appName}',
        options: const AuthenticationOptions(
          stickyAuth: true,
          biometricOnly: false,
        ),
      );
    } on Exception {
      // If the platform auth call errors, fail closed only when we know a
      // token exists — but here we return false so the lock screen can retry.
      return false;
    }
  }
}
