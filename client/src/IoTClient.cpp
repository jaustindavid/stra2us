#include "IoTClient.h"
#include <string.h>

// On ESP32, mbedtls is built-in.
#if defined(ESP32) || defined(PARTICLE)
#include "mbedtls/md.h"
#else
// A fallback would be needed for regular Arduino Uno, 
// e.g. using Arduino Cryptography Library:
// #include <Crypto.h>
// #include <SHA256.h>
#endif

// CMP Buffer Callbacks
static bool mem_buf_reader(cmp_ctx_t *ctx, void *data, size_t limit) {
    return false; // Not implemented for simple memory buffer here
}
static bool mem_buf_skipper(cmp_ctx_t *ctx, size_t count) { return false; }

static size_t mem_buf_writer(cmp_ctx_t *ctx, const void *data, size_t count) {
    struct memory_buffer *mb = (struct memory_buffer *)ctx->buf;
    if (mb->size + count > mb->capacity) {
        return 0; // Overflow
    }
    memcpy(mb->data + mb->size, data, count);
    mb->size += count;
    return count;
}

void init_memory_buffer(struct memory_buffer* mb, uint8_t* mem, size_t capacity) {
    mb->data = mem;
    mb->size = 0;
    mb->capacity = capacity;
}

IoTClient::IoTClient(Client& client, const char* host, uint16_t port, const char* clientId, const char* secretHex)
    : _client(client), _host(host), _port(port), _clientId(clientId), _secretHex(secretHex), _timeFunc(nullptr), _lastResponseTs(0) {
}

void IoTClient::setTimeFunction(uint32_t (*timeFunc)()) {
    _timeFunc = timeFunc;
}

void IoTClient::calculateSignature(const char* uri, const uint8_t* payload, size_t payloadLen, uint32_t timestamp, char* outHex) {
#if defined(ESP32) || defined(PARTICLE)
    mbedtls_md_context_t ctx;
    mbedtls_md_init(&ctx);

    const mbedtls_md_info_t* md_info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    if (!md_info) return;

    mbedtls_md_setup(&ctx, md_info, 1);

    // Convert hex secret to bytes
    uint8_t secretBytes[32];
    for (int i = 0; i < 32; i++) {
        char octet[3] = {_secretHex[i * 2], _secretHex[i * 2 + 1], '\0'};
        secretBytes[i] = (uint8_t)strtol(octet, NULL, 16);
    }

    mbedtls_md_hmac_starts(&ctx, secretBytes, 32);

    // HMAC over: URI + Body + Timestamp
    mbedtls_md_hmac_update(&ctx, (const unsigned char*)uri, strlen(uri));
    if (payloadLen > 0 && payload != nullptr) {
        mbedtls_md_hmac_update(&ctx, payload, payloadLen);
    }
    
    char tsStr[16];
    snprintf(tsStr, sizeof(tsStr), "%lu", (unsigned long)timestamp);
    mbedtls_md_hmac_update(&ctx, (const unsigned char*)tsStr, strlen(tsStr));

    uint8_t hmacResult[32];
    mbedtls_md_hmac_finish(&ctx, hmacResult);
    mbedtls_md_free(&ctx);

    // Convert to hex
    for (int i = 0; i < 32; i++) {
        sprintf(&outHex[i * 2], "%02x", hmacResult[i]);
    }
    outHex[64] = '\0';
#else
    // Dummy placeholder for non-ESP32
    strcpy(outHex, "mock_signature_for_testing");
#endif
}

int IoTClient::sendSignedRequest(const char* method, const char* uri, const uint8_t* payload, size_t payloadLen, uint8_t* responseBuffer, size_t maxLen, size_t* outLen, const char* contentType) {
    if (!_client.connected()) {
        if (!_client.connect(_host, _port)) {
            return -1;
        }
    }

    uint32_t ts = _timeFunc ? _timeFunc() : 0;
    char sigHex[65];
    calculateSignature(uri, payload, payloadLen, ts, sigHex);

    // Write HTTP Request with zero-malloc technique
    _client.print(method);
    _client.print(" ");
    _client.print(uri);
    _client.print(" HTTP/1.1\r\n");

    _client.print("Host: ");
    _client.print(_host);
    _client.print("\r\n");
    
    _client.print("Connection: keep-alive\r\n");

    _client.print("X-Client-ID: ");
    _client.print(_clientId);
    _client.print("\r\n");

    _client.print("X-Timestamp: ");
    _client.print(ts);
    _client.print("\r\n");

    _client.print("X-Signature: ");
    _client.print(sigHex);
    _client.print("\r\n");

    if (payloadLen > 0) {
        _client.print("Content-Type: ");
        _client.print(contentType);
        _client.print("\r\n");
        _client.print("Content-Length: ");
        _client.print(payloadLen);
        _client.print("\r\n\r\n");
        _client.write(payload, payloadLen);
    } else {
        _client.print("Content-Length: 0\r\n\r\n");
    }

    // Await Response briefly
    unsigned long start = millis();
    while(!_client.available() && millis() - start < 5000) {
        delay(10);
    }

    // HTTP/1.1 Keep-Alive response processing
    bool isBody = false;
    String currentLine = "";
    int readHead = 0;
    int contentLength = -1;
    int statusCode = -1;
    bool isFirstLine = true;
    
    while (_client.connected() || _client.available()) {
        if (_client.available()) {
            char c = _client.read();
            if (!isBody) {
                currentLine += c;
                if (currentLine.endsWith("\n")) {
                    if (isFirstLine) {
                        // Parse "HTTP/1.1 200 OK"
                        int firstSpace = currentLine.indexOf(' ');
                        if (firstSpace != -1) {
                            String codePart = currentLine.substring(firstSpace + 1);
                            int secondSpace = codePart.indexOf(' ');
                            if (secondSpace != -1) {
                                statusCode = codePart.substring(0, secondSpace).toInt();
                            } else {
                                statusCode = codePart.toInt();
                            }
                        }
                        isFirstLine = false;
                    }

                    String lowerLine = currentLine;
                    lowerLine.toLowerCase();
                    if (lowerLine.startsWith("content-length:")) {
                        contentLength = lowerLine.substring(15).toInt();
                    }
                    // Capture X-Response-Timestamp so encrypted-KV reads can
                    // use it as the keystream nonce (see decryptKVResponseIfEncrypted).
                    // Stored regardless of HTTP status — the timestamp is
                    // signed in tandem with the body so it's safe to use
                    // even on 204/4xx responses.
                    if (lowerLine.startsWith("x-response-timestamp:")) {
                        _lastResponseTs = (uint32_t)lowerLine.substring(21).toInt();
                    }
                    if (currentLine == "\r\n" || currentLine == "\n") {
                        isBody = true;
                        if (contentLength == 0) {
                            break; // 204 No Content or Empty
                        }
                    }
                    currentLine = "";
                }
            } else {
                if (responseBuffer != nullptr && outLen != nullptr && readHead < maxLen) {
                    responseBuffer[readHead] = c;
                }
                readHead++;
                
                if (contentLength >= 0 && readHead >= contentLength) {
                    break; // Finished reading HTTP body
                }
            }
        } else {
            if (!_client.connected()) break;
            delay(1);
        }
    }
    
    if (outLen != nullptr) {
        *outLen = readHead;
    }

    // We purposely do NOT call _client.stop() to preserve the TCP socket
    return statusCode;
}

int IoTClient::writeKV(const char* key, const uint8_t* payload, size_t payloadLen) {
    char uri[64];
    snprintf(uri, sizeof(uri), "/kv/%s", key);
    return sendSignedRequest("POST", uri, payload, payloadLen, nullptr, 0, nullptr);
}

int IoTClient::readKV(const char* key, uint8_t* responseBuffer, size_t maxLen, size_t* outLen) {
    char uri[64];
    snprintf(uri, sizeof(uri), "/kv/%s", key);
    return sendSignedRequest("GET", uri, nullptr, 0, responseBuffer, maxLen, outLen);
}

int IoTClient::publishQueue(const char* topic, const uint8_t* payload, size_t payloadLen) {
    char uri[64];
    snprintf(uri, sizeof(uri), "/q/%s", topic);
    return sendSignedRequest("POST", uri, payload, payloadLen, nullptr, 0, nullptr);
}

int IoTClient::publishQueue(const char* topic, const uint8_t* payload, size_t payloadLen, uint32_t ttl) {
    char uri[128];
    snprintf(uri, sizeof(uri), "/q/%s?ttl=%lu", topic, (unsigned long)ttl);
    return sendSignedRequest("POST", uri, payload, payloadLen, nullptr, 0, nullptr);
}

int IoTClient::publishQueue(const char* topic, const char* message) {
    char uri[64];
    snprintf(uri, sizeof(uri), "/q/%s", topic);
    return sendSignedRequest("POST", uri, (const uint8_t*)message, strlen(message), nullptr, 0, nullptr, "text/plain");
}

int IoTClient::consumeQueue(const char* topic, uint8_t* responseBuffer, size_t maxLen, size_t* outLen, bool envelope) {
    char uri[96];
    if (envelope) {
        snprintf(uri, sizeof(uri), "/q/%s?envelope=true", topic);
    } else {
        snprintf(uri, sizeof(uri), "/q/%s", topic);
    }

    size_t localOutLen = 0;
    int status = sendSignedRequest("GET", uri, nullptr, 0, responseBuffer, maxLen, &localOutLen);

    if (outLen != nullptr) {
        *outLen = localOutLen;
    }

    return status;
}

// ---- Per-key encrypted KV values (see docs/fr_encrypted_values.md) ----

void IoTClient::kvencXor(uint32_t nonce, uint8_t* data, size_t len) {
#if defined(ESP32) || defined(PARTICLE)
    // Decode 32-byte secret from hex (mirrors calculateSignature).
    uint8_t secretBytes[32];
    for (int i = 0; i < 32; i++) {
        char hexByte[3] = { _secretHex[i * 2], _secretHex[i * 2 + 1], '\0' };
        secretBytes[i] = (uint8_t)strtol(hexByte, nullptr, 16);
    }

    // Domain-separation label — must match server's KVENC_LABEL exactly.
    static const char kLabel[] = "stra2us-kvenc-v1";
    static const size_t kLabelLen = sizeof(kLabel) - 1;  // 16, no NUL

    // nonce as uint32 BE.
    uint8_t nonceBytes[4] = {
        (uint8_t)((nonce >> 24) & 0xFF),
        (uint8_t)((nonce >> 16) & 0xFF),
        (uint8_t)((nonce >> 8)  & 0xFF),
        (uint8_t)(nonce & 0xFF),
    };

    const mbedtls_md_info_t* md_info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    if (!md_info) return;  // Defensive — mbedtls misconfig

    uint8_t counter = 0;
    size_t pos = 0;
    while (pos < len) {
        // keystream block = HMAC-SHA256(secret, label || nonce_be32 || counter)
        mbedtls_md_context_t ctx;
        mbedtls_md_init(&ctx);
        mbedtls_md_setup(&ctx, md_info, 1);
        mbedtls_md_hmac_starts(&ctx, secretBytes, 32);
        mbedtls_md_hmac_update(&ctx, (const unsigned char*)kLabel, kLabelLen);
        mbedtls_md_hmac_update(&ctx, nonceBytes, 4);
        mbedtls_md_hmac_update(&ctx, &counter, 1);
        uint8_t block[32];
        mbedtls_md_hmac_finish(&ctx, block);
        mbedtls_md_free(&ctx);

        size_t blockEnd = pos + 32;
        if (blockEnd > len) blockEnd = len;
        for (size_t i = pos; i < blockEnd; i++) {
            data[i] ^= block[i - pos];
        }
        pos = blockEnd;
        counter++;
        // 256 * 32 = 8192 bytes is the practical ceiling for one nonce.
        // No KV value will ever approach that — the server enforces a similar
        // cap. Defensive break in case of bug:
        if (counter == 0 && pos < len) return;  // wrapped past 255
    }
#else
    // No mbedtls fallback — caller is responsible for not enabling
    // encrypted KVs on platforms without HMAC-SHA256.
    (void)nonce; (void)data; (void)len;
#endif
}

bool IoTClient::decryptKVResponseIfEncrypted(uint8_t* data, size_t* len) {
    if (data == nullptr || len == nullptr || *len < 2) return false;

    // Manual msgpack ext-family parse. Avoids dragging in cmp's reader
    // infrastructure for one tiny header check. See msgpack spec §ext.
    //
    // Layouts (header bytes followed by `payloadLen` bytes of ciphertext):
    //   0xd4 type [1 byte ]   fixext1
    //   0xd5 type [2 bytes]   fixext2
    //   0xd6 type [4 bytes]   fixext4
    //   0xd7 type [8 bytes]   fixext8
    //   0xd8 type [16 bytes]  fixext16
    //   0xc7 size(u8)  type [size bytes]   ext8
    //   0xc8 size(u16) type [size bytes]   ext16  (size big-endian)
    //   0xc9 size(u32) type [size bytes]   ext32  (size big-endian)
    //
    // We only treat type == 0x21 (KVENC_EXT_TYPE) as encrypted. Anything
    // else falls through unchanged so callers can still get e.g. plaintext
    // msgpack maps for `{"status": "not_found"}` responses.

    uint8_t marker = data[0];
    size_t payloadLen = 0;
    size_t headerLen = 0;
    int8_t extType = 0;

    switch (marker) {
        case 0xd4: payloadLen = 1;  headerLen = 2; extType = (int8_t)data[1]; break;
        case 0xd5: payloadLen = 2;  headerLen = 2; extType = (int8_t)data[1]; break;
        case 0xd6: payloadLen = 4;  headerLen = 2; extType = (int8_t)data[1]; break;
        case 0xd7: payloadLen = 8;  headerLen = 2; extType = (int8_t)data[1]; break;
        case 0xd8: payloadLen = 16; headerLen = 2; extType = (int8_t)data[1]; break;
        case 0xc7:
            if (*len < 3) return false;
            payloadLen = data[1];
            extType = (int8_t)data[2];
            headerLen = 3;
            break;
        case 0xc8:
            if (*len < 4) return false;
            payloadLen = ((size_t)data[1] << 8) | (size_t)data[2];
            extType = (int8_t)data[3];
            headerLen = 4;
            break;
        case 0xc9:
            if (*len < 6) return false;
            payloadLen = ((size_t)data[1] << 24) | ((size_t)data[2] << 16) |
                         ((size_t)data[3] << 8)  | (size_t)data[4];
            extType = (int8_t)data[5];
            headerLen = 6;
            break;
        default:
            return false;  // Not an ext type — leave caller's buffer alone.
    }

    if (extType != 0x21) return false;
    if (headerLen + payloadLen > *len) return false;  // Truncated — refuse.

    // Decrypt in place, then collapse the msgpack ext header out so the
    // caller's buffer holds just the plaintext bytes (matches what the
    // CLI's get() returns post-decrypt).
    kvencXor(_lastResponseTs, data + headerLen, payloadLen);
    if (headerLen > 0) {
        memmove(data, data + headerLen, payloadLen);
    }
    *len = payloadLen;
    return true;
}
