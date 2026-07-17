/// Tests for the credential + unlock owner of the ops app.
///
/// This is the security seam of the owner's control-plane phone app: the
/// app-token lives only in keystore-backed secure storage, the login
/// exchange must never store anything on failure, and the biometric gate
/// has deliberate fail-open (no enrolled auth → usable) and fail-closed
/// (platform error → locked, retryable) branches.  All of that was
/// unpinned until now.
///
/// Secure storage and local_auth are faked at their injection seams; the
/// login exchange runs against a real loopback HTTP server so the wire
/// contract (`POST /api/v1/auth/login`) is exercised as shipped.
library;

import 'dart:convert';
import 'dart:io';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:local_auth/local_auth.dart';
import 'package:ops360/api/api_client.dart';
import 'package:ops360/auth/auth_service.dart';

class _FakeStorage extends Fake implements FlutterSecureStorage {
  final Map<String, String> data = {};

  @override
  Future<String?> read({
    required String key,
    IOSOptions? iOptions,
    AndroidOptions? aOptions,
    LinuxOptions? lOptions,
    WebOptions? webOptions,
    MacOsOptions? mOptions,
    WindowsOptions? wOptions,
  }) async =>
      data[key];

  @override
  Future<void> write({
    required String key,
    required String? value,
    IOSOptions? iOptions,
    AndroidOptions? aOptions,
    LinuxOptions? lOptions,
    WebOptions? webOptions,
    MacOsOptions? mOptions,
    WindowsOptions? wOptions,
  }) async {
    if (value == null) {
      data.remove(key);
    } else {
      data[key] = value;
    }
  }

  @override
  Future<void> delete({
    required String key,
    IOSOptions? iOptions,
    AndroidOptions? aOptions,
    LinuxOptions? lOptions,
    WebOptions? webOptions,
    MacOsOptions? mOptions,
    WindowsOptions? wOptions,
  }) async {
    data.remove(key);
  }
}

class _FakeLocalAuth extends Fake implements LocalAuthentication {
  _FakeLocalAuth({
    this.deviceSupported = true,
    this.canCheck = true,
    this.enrolled = const [BiometricType.fingerprint],
    this.authResult = true,
    this.throwEverywhere = false,
  });

  bool deviceSupported;
  bool canCheck;
  List<BiometricType> enrolled;
  bool authResult;
  bool throwEverywhere;
  int authenticateCalls = 0;

  void _maybeThrow() {
    if (throwEverywhere) throw Exception('platform channel down');
  }

  @override
  Future<bool> isDeviceSupported() async {
    _maybeThrow();
    return deviceSupported;
  }

  @override
  Future<bool> get canCheckBiometrics async {
    _maybeThrow();
    return canCheck;
  }

  @override
  Future<List<BiometricType>> getAvailableBiometrics() async {
    _maybeThrow();
    return enrolled;
  }

  @override
  Future<bool> authenticate({
    required String localizedReason,
    // Widened to Object: AuthMessages isn't exported by local_auth's public
    // surface, and a supertype is a legal override parameter type.
    Iterable<Object> authMessages = const <Object>[],
    AuthenticationOptions options = const AuthenticationOptions(),
  }) async {
    _maybeThrow();
    authenticateCalls++;
    return authResult;
  }
}

void main() {
  group('load / logout', () {
    test('empty storage means logged out', () async {
      final auth = AuthService(
          storage: _FakeStorage(), localAuth: _FakeLocalAuth());
      expect(await auth.load(), isFalse);
      expect(auth.isLoggedIn, isFalse);
    });

    test('persisted credentials restore the session', () async {
      final storage = _FakeStorage()
        ..data['ops_base_url'] = 'https://ops.example.org'
        ..data['ops_app_token'] = 'tok-1';
      final auth = AuthService(storage: storage, localAuth: _FakeLocalAuth());
      expect(await auth.load(), isTrue);
      expect(auth.baseUrl, 'https://ops.example.org');
    });

    test('logout clears the token but keeps the base URL', () async {
      final storage = _FakeStorage()
        ..data['ops_base_url'] = 'https://ops.example.org'
        ..data['ops_app_token'] = 'tok-1';
      final auth = AuthService(storage: storage, localAuth: _FakeLocalAuth());
      await auth.load();
      await auth.logout();
      expect(auth.isLoggedIn, isFalse);
      expect(storage.data.containsKey('ops_app_token'), isFalse);
      // Base URL survives so the next login pre-fills the server field.
      expect(storage.data['ops_base_url'], 'https://ops.example.org');
    });
  });

  group('login exchange', () {
    late HttpServer server;
    late String base;
    late int status;
    late Object payload;
    late List<String> paths;

    setUp(() async {
      status = 200;
      payload = {'token': 'fresh-token'};
      paths = [];
      server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
      base = 'http://127.0.0.1:${server.port}';
      server.listen((req) async {
        paths.add(req.uri.path);
        await utf8.decoder.bind(req).join();
        req.response.statusCode = status;
        req.response.headers.contentType = ContentType.json;
        req.response.write(jsonEncode(payload));
        await req.response.close();
      });
    });

    tearDown(() => server.close(force: true));

    test('success persists normalized base URL + token', () async {
      final storage = _FakeStorage();
      final auth = AuthService(storage: storage, localAuth: _FakeLocalAuth());
      await auth.login(baseUrl: '$base///', password: 'pw');
      expect(paths.single, '/api/v1/auth/login');
      expect(auth.isLoggedIn, isTrue);
      // Trailing slashes are stripped before the URL is stored.
      expect(storage.data['ops_base_url'], base);
      expect(storage.data['ops_app_token'], 'fresh-token');
    });

    test('wrong password throws and stores nothing', () async {
      status = 401;
      payload = {'detail': 'bad'};
      final storage = _FakeStorage();
      final auth = AuthService(storage: storage, localAuth: _FakeLocalAuth());
      await expectLater(
        auth.login(baseUrl: base, password: 'nope'),
        throwsA(isA<ApiException>()
            .having((e) => e.message, 'message', 'Wrong password.')),
      );
      expect(auth.isLoggedIn, isFalse);
      expect(storage.data, isEmpty);
    });

    test('200 without a token is rejected as unexpected', () async {
      payload = {'ok': true};
      final auth = AuthService(
          storage: _FakeStorage(), localAuth: _FakeLocalAuth());
      await expectLater(
        auth.login(baseUrl: base, password: 'pw'),
        throwsA(isA<ApiException>()
            .having((e) => e.message, 'message', contains('Unexpected'))),
      );
    });

    test('unreachable server maps to a friendly message', () async {
      final auth = AuthService(
          storage: _FakeStorage(), localAuth: _FakeLocalAuth());
      await expectLater(
        auth.login(baseUrl: 'http://127.0.0.1:1', password: 'pw'),
        throwsA(isA<ApiException>()
            .having((e) => e.message, 'message', contains('Cannot reach'))),
      );
    });

    test('revoke-all logs out locally even when the server call fails',
        () async {
      // Lost-phone switch: the local token must die regardless of whether
      // the revoke round-trip succeeded.
      status = 500;
      payload = {'detail': 'boom'};
      final storage = _FakeStorage()
        ..data['ops_base_url'] = base
        ..data['ops_app_token'] = 'tok-1';
      final auth = AuthService(storage: storage, localAuth: _FakeLocalAuth());
      await auth.load();
      expect(auth.isLoggedIn, isTrue);
      await expectLater(
          auth.revokeAllDevices(), throwsA(isA<ApiException>()));
      expect(auth.isLoggedIn, isFalse);
      expect(storage.data.containsKey('ops_app_token'), isFalse);
    });
  });

  group('biometric gate', () {
    test('app-lock defaults to OFF', () async {
      final auth = AuthService(
          storage: _FakeStorage(), localAuth: _FakeLocalAuth());
      expect(await auth.biometricEnabled(), isFalse);
      await auth.setBiometricEnabled(true);
      expect(await auth.biometricEnabled(), isTrue);
    });

    test('biometricsAvailable is false on unsupported devices', () async {
      final auth = AuthService(
        storage: _FakeStorage(),
        localAuth: _FakeLocalAuth(deviceSupported: false),
      );
      expect(await auth.biometricsAvailable(), isFalse);
    });

    test('biometricsAvailable needs an actual enrollment', () async {
      final auth = AuthService(
        storage: _FakeStorage(),
        localAuth: _FakeLocalAuth(enrolled: const []),
      );
      expect(await auth.biometricsAvailable(), isFalse);
    });

    test('biometricsAvailable swallows platform errors as false', () async {
      final auth = AuthService(
        storage: _FakeStorage(),
        localAuth: _FakeLocalAuth(throwEverywhere: true),
      );
      expect(await auth.biometricsAvailable(), isFalse);
    });

    test('unlock passes through on devices with nothing to gate on',
        () async {
      // Fail-open by design: no enrolled auth must not brick the app.
      final unsupported = AuthService(
        storage: _FakeStorage(),
        localAuth: _FakeLocalAuth(deviceSupported: false),
      );
      expect(await unsupported.unlock(), isTrue);

      final noEnrollment = AuthService(
        storage: _FakeStorage(),
        localAuth:
            _FakeLocalAuth(canCheck: false, enrolled: const []),
      );
      expect(await noEnrollment.unlock(), isTrue);
    });

    test('unlock prompts and returns the platform verdict', () async {
      final local = _FakeLocalAuth(authResult: true);
      final auth = AuthService(storage: _FakeStorage(), localAuth: local);
      expect(await auth.unlock(), isTrue);
      expect(local.authenticateCalls, 1);

      local.authResult = false;
      expect(await auth.unlock(), isFalse);
    });

    test('unlock fails closed on a platform error so the lock screen can '
        'retry', () async {
      final auth = AuthService(
        storage: _FakeStorage(),
        localAuth: _FakeLocalAuth(throwEverywhere: true),
      );
      expect(await auth.unlock(), isFalse);
    });
  });
}
