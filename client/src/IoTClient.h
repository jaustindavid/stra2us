#ifndef IOT_CLIENT_H
#define IOT_CLIENT_H

#include <Arduino.h>
#include <Client.h>
#include "cmp.h"

class IoTClient {
public:
    IoTClient(Client& client, const char* host, uint16_t port, const char* clientId, const char* secretHex);

    // Write persistent configuration data to the KV store
    int writeKV(const char* key, const uint8_t* payload, size_t payloadLen);

    // Read persistent configuration data from the KV store
    int readKV(const char* key, uint8_t* responseBuffer, size_t maxLen, size_t* outLen);

    // Publish ephemeral data to a queue (uses backend default TTL of 1 hour)
    int publishQueue(const char* topic, const uint8_t* payload, size_t payloadLen);

    // Publish ephemeral data to a queue with a custom TTL (in seconds)
    int publishQueue(const char* topic, const uint8_t* payload, size_t payloadLen, uint32_t ttl);

    // [New] Publish ephemeral data as an un-encoded raw UTF-8 string (uses text/plain)
    int publishQueue(const char* topic, const char* message);

    // Consume messages from a queue using the pulling cursor.
    // Returns HTTP status code (200 = msg, 204 = empty, etc.) or -1 on error.
    // When envelope=true, the response is a msgpack-packed dict:
    //   {"data": <payload>, "client_id": "<publisher>", "received_at": <unix seconds>}
    int consumeQueue(const char* topic, uint8_t* responseBuffer, size_t maxLen, size_t* outLen, bool envelope = false);

    // Set a function to get unix time (required for timestamp signing)
    void setTimeFunction(uint32_t (*timeFunc)());

    // Utility: calculate HMAC-SHA256 (exposed for advanced use or internal)
    void calculateSignature(const char* uri, const uint8_t* payload, size_t payloadLen, uint32_t timestamp, char* outHex);

    // ---- Per-key encrypted KV values (see docs/fr_encrypted_values.md) ----
    //
    // Server-side opt-in: a KV record marked `encrypted` is delivered to
    // device GETs as a msgpack ext type 0x21 wrapping HMAC-keystream
    // ciphertext. The keystream is keyed off this client's shared secret
    // and uses the response's X-Response-Timestamp as the per-call nonce.
    //
    // Typical use after `readKV`:
    //
    //     uint8_t buf[128]; size_t len;
    //     int s = client.readKV("wifi_password", buf, sizeof(buf), &len);
    //     if (s == 200) {
    //         client.decryptKVResponseIfEncrypted(buf, &len);
    //         // `buf[0..len)` is now plaintext bytes regardless of whether
    //         // the server sent an encrypted record.
    //     }
    //
    // The convenience wrapper handles ext-type detection and length update.
    // Use the lower-level `kvencXor` if you've already extracted the
    // ciphertext bytes yourself.

    // X-Response-Timestamp from the most recent successful response, or 0
    // if none has been seen yet. Used as the keystream nonce.
    uint32_t lastResponseTimestamp() const { return _lastResponseTs; }

    // HMAC-SHA256 keystream cipher, in-place XOR of `data[0..len)`.
    // `nonce` is the response timestamp (typically lastResponseTimestamp()).
    // Symmetric — same call encrypts and decrypts. Mirrors server's
    // `core/security.py:kvenc_xor` byte-for-byte.
    void kvencXor(uint32_t nonce, uint8_t* data, size_t len);

    // If `data[0..*len)` parses as msgpack ext type 0x21, decrypt the
    // ciphertext payload in place, overwrite `data[0..)` with the plaintext
    // bytes (note: the original msgpack ext framing is dropped — caller
    // gets just the inner bytes), update `*len` to the plaintext length,
    // and return true. Otherwise leaves `data`/`len` unchanged and returns
    // false. Uses lastResponseTimestamp() as the nonce.
    bool decryptKVResponseIfEncrypted(uint8_t* data, size_t* len);

private:
    Client& _client;
    const char* _host;
    uint16_t _port;
    const char* _clientId;
    const char* _secretHex;

    uint32_t (*_timeFunc)();
    uint32_t _lastResponseTs;

    int sendSignedRequest(const char* method, const char* uri, const uint8_t* payload, size_t payloadLen, uint8_t* responseBuffer, size_t maxLen, size_t* outLen, const char* contentType = "application/x-msgpack");
    void readResponse(uint8_t* responseBuffer, size_t maxLen, size_t* outLen);
};

// Custom minimal buffer writer for cmp
struct memory_buffer {
    uint8_t* data;
    size_t size;
    size_t capacity;
};

// Helper initialization
void init_memory_buffer(struct memory_buffer* buf, uint8_t* mem, size_t capacity);

#endif // IOT_CLIENT_H
