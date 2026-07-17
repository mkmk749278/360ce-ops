/// End-to-end tests for the ops `/api/v1` client against a real local HTTP
/// server (no mock adapter — the client builds its own Dio, so we exercise
/// the wire behaviour exactly as the app ships it).
///
/// What matters here is the auth + error contract every screen relies on:
///
/// * the app-token travels as `Authorization: Bearer <token>` on every verb;
/// * 401 → typed [UnauthorizedException] (the "bounce to login" signal);
/// * other 4xx → [ApiException] carrying the ops `detail` when present;
/// * 2xx returns decoded JSON;
/// * connection failures surface as a friendly [ApiException], never a raw
///   DioException the UI can't render.
library;

import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:ops360/api/api_client.dart';

void main() {
  late HttpServer server;
  late Uri base;
  // Each entry: the handler decides status + body; requests are recorded so
  // tests can assert on method/path/headers.
  late List<HttpRequest> seen;
  late int status;
  late Object body;

  setUp(() async {
    seen = [];
    status = 200;
    body = {'ok': true};
    server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    base = Uri.parse('http://127.0.0.1:${server.port}');
    server.listen((req) async {
      seen.add(req);
      // Drain the body so the client isn't left hanging.
      await utf8.decoder.bind(req).join();
      req.response.statusCode = status;
      req.response.headers.contentType = ContentType.json;
      req.response.write(jsonEncode(body));
      await req.response.close();
    });
  });

  tearDown(() async {
    await server.close(force: true);
  });

  ApiClient client({String? token = 'app-token-1'}) =>
      ApiClient(baseUrl: base.toString(), token: token);

  group('request shape', () {
    test('GET hits /api/v1 prefix with bearer token', () async {
      await client().get('/pulse');
      expect(seen.single.uri.path, '/api/v1/pulse');
      expect(seen.single.headers.value('authorization'), 'Bearer app-token-1');
    });

    test('query parameters are forwarded', () async {
      await client().get('/signals', query: {'status': 'open'});
      expect(seen.single.uri.queryParameters['status'], 'open');
    });

    test('POST carries the bearer token too', () async {
      await client().post('/control/kill-switch', body: {'engaged': true});
      expect(seen.single.method, 'POST');
      expect(seen.single.headers.value('authorization'), 'Bearer app-token-1');
    });

    test('no token means no Authorization header', () async {
      await client(token: null).get('/pulse');
      expect(seen.single.headers.value('authorization'), isNull);
    });

    test('setToken swaps the header for subsequent requests', () async {
      final c = client();
      c.setToken('rotated');
      await c.get('/pulse');
      expect(seen.single.headers.value('authorization'), 'Bearer rotated');
    });
  });

  group('response contract', () {
    test('2xx returns decoded JSON', () async {
      body = {'mode': 'paper'};
      final data = await client().get('/control/state');
      expect(data, {'mode': 'paper'});
    });

    test('401 throws UnauthorizedException (bounce-to-login signal)', () async {
      status = 401;
      expect(client().get('/pulse'), throwsA(isA<UnauthorizedException>()));
    });

    test('4xx throws ApiException carrying the ops detail', () async {
      status = 422;
      body = {'detail': 'mode must be off|paper|live'};
      expect(
        client().post('/control/auto-mode', body: {'mode': 'turbo'}),
        throwsA(
          isA<ApiException>().having(
            (e) => e.message,
            'message',
            contains('mode must be off|paper|live'),
          ),
        ),
      );
    });

    test('4xx without detail still reports the status code', () async {
      status = 403;
      body = {};
      expect(
        client().get('/pulse'),
        throwsA(
          isA<ApiException>()
              .having((e) => e.message, 'message', contains('403')),
        ),
      );
    });

    test('connection failure surfaces a friendly ApiException', () async {
      // Point at a closed port: connection refused, not a raw DioException.
      final dead = ApiClient(baseUrl: 'http://127.0.0.1:1', token: 't');
      expect(dead.get('/pulse'), throwsA(isA<ApiException>()));
    });
  });
}
