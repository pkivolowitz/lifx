# HAP-over-CoAP Wire Format Reference

Source: `aiohomekit` `main` branch, `aiohomekit/controller/coap/` plus
`aiohomekit/protocol/__init__.py`. Citations are file:line. Snippets are
verbatim except where noted.

---

## 1. CoAP URI / method structure

All HAP-CoAP operations are CoAP **POST** to a numeric path on the
accessory, addressed as a literal IPv6 URI (`coap://[<host>]:<port>/<n>`).
The transport is plain CoAP-over-UDP — aiohomekit does not negotiate DTLS;
HAP's own ChaCha20Poly1305 layer is applied to the CoAP payload after
pair-verify completes. Confirmable / non-confirmable is left to `aiocoap`
defaults (Confirmable for POST), no observe is used.

| Operation | Path | Method | Encrypted? | Notes |
|---|---|---|---|---|
| Identify (unpaired) | `/0` | POST | no | empty body, expects `2.04 Changed` (`connection.py:252-263`) |
| Pair-Setup (M1..M6) | `/1` | POST | no | TLV body, multi-RT (`connection.py:267, 291`) |
| Pair-Verify (M1..M4) | `/2` | POST | no | TLV body, 2 RTs (`connection.py:328`) |
| All paired ops | `/` (root) | POST | yes | encrypted PDU body (`connection.py:361, 158`) |

Once pair-verify completes, **everything funnels through one URI** —
`coap://[host]:port/` — and the operation is encoded in the **PDU opcode
inside the encrypted body**, not in the URI. List-accessories,
read/write characteristics, subscribe, unsubscribe, list-pairings,
remove-pairing all hit `/`. See `connection.py:361` (URI is set once)
and the `OpCode` table in §4.

Server response codes: `2.04 CHANGED` is success
(`connection.py:173`); `4.04 NOT_FOUND` after pair-verify means the
session is gone — accessory rebooted, must re-pair-verify
(`connection.py:168-172`).

**Content-format**: aiohomekit does **not** set any Content-Format
option; payloads are raw bytes (TLV during setup/verify, encrypted PDU
afterwards). `Message(code=Code.POST, payload=..., uri=...)` is
constructed without options (`connection.py:158, 275, 299, 337`). If
your peer demands a CF code, this codebase does not document one —
treat as opaque application/octet-stream.

**Identify** is unauthenticated POST `/0` with empty body; success is
`2.04 Changed` (`connection.py:252-263`).

**Subscribe / unsubscribe / list-pairings / remove-pairing** are *not*
distinct URIs — they ride encrypted PDUs over `/`. See §4 and §5.

---

## 2. Pair-verify over CoAP

Two POST round trips on `/2`. The TLV state-machine is the *same*
`get_session_keys` generator used by HAP-IP/HAP-BLE
(`protocol/__init__.py:430`); CoAP merely frames each yielded TLV
list as one POST body and feeds the response TLV back.

```
Round trip 1 (M1 -> M2)
  iOS  -> Acc:  TLV { State=M1, PublicKey=iOS_X25519_pub }
  Acc  -> iOS:  TLV { State=M2, PublicKey=Acc_X25519_pub,
                      EncryptedData=<sub-TLV{Identifier,Signature}>+tag }

Round trip 2 (M3 -> M4)
  iOS  -> Acc:  TLV { State=M3,
                      EncryptedData=<sub-TLV{Identifier,Signature}>+tag }
  Acc  -> iOS:  TLV { State=M4 [, Error] }
```

`connection.py:330-352` is the loop; `protocol/__init__.py:455-580` is
the cryptographic state machine. Resume (`kTLVMethod_Resume`) is
handled inside `get_session_keys` and can short-circuit pair-verify if a
session_id from a previous session is supplied
(`protocol/__init__.py:462-464, 479-481`).

### 2a. HKDF salt/info strings (these are the deltas vs. HAP-BLE)

After the X25519 ECDH `shared_secret = ios_key.exchange(acc_pub)`
(`protocol/__init__.py:492`) the same `shared_secret` is fed through
HKDF-SHA512 (the project's `hkdf_derive`) with several distinct
salt/info pairs. **The CoAP stack derives THREE keys**, not two:

| Key | Salt | Info | File:line |
|---|---|---|---|
| Pair-verify intermediate (decrypt M2 sub-TLV, encrypt M3 sub-TLV) | `Pair-Verify-Encrypt-Salt` | `Pair-Verify-Encrypt-Info` | `protocol/__init__.py:495` |
| Session resume id (8 bytes) | `Pair-Verify-ResumeSessionID-Salt` | `Pair-Verify-ResumeSessionID-Info` | `protocol/__init__.py:575-578` |
| Accessory -> controller (recv) | `Control-Salt` | `Control-Read-Encryption-Key` | `connection.py:354` |
| Controller -> accessory (send) | `Control-Salt` | `Control-Write-Encryption-Key` | `connection.py:356` |
| Event channel (acc -> controller PUT) | `Event-Salt` | `Event-Read-Encryption-Key` | `connection.py:358` |

**Compare to HAP-BLE**: HAP-BLE uses `Control-Salt` /
`Control-Read-Encryption-Key` and `Control-Write-Encryption-Key` for the
two paired keys — those two are **identical** to CoAP. HAP-BLE has **no
event key**; CoAP adds the third `Event-Salt / Event-Read-Encryption-Key`
key for asynchronous PUT-notifications (see §5). Pair-verify intermediate
HKDF labels (`Pair-Verify-Encrypt-Salt/Info`) are also identical across
transports; this is generic HAP, not CoAP-specific.

The M2 sub-TLV is decrypted with the intermediate `session_key` using
nonce `NONCE_PADDING + b"PV-Msg02"` (32-bit zero pad + 8-byte ASCII
label, total 12 bytes), and M3 sub-TLV is encrypted with
`NONCE_PADDING + b"PV-Msg03"` (`protocol/__init__.py:501, 553`). Same
labels HAP-BLE uses — no CoAP delta here.

### 2b. Pair-setup (one-time)

POST `/1`, identical TLV M1..M6 state machine
(`perform_pair_setup_part1` and `perform_pair_setup_part2`,
`protocol/__init__.py`). HKDF labels are stock HAP:
`Pair-Setup-Encrypt-Salt/Info`,
`Pair-Setup-Controller-Sign-Salt/Info`,
`Pair-Setup-Accessory-Sign-Salt/Info`
(`protocol/__init__.py:241-248, 322-323`).

---

## 3. Encrypted-frame layout (post-pair-verify)

After pair-verify, every CoAP body sent on `/` is the **CoAP payload =
ChaCha20Poly1305(plaintext_PDU)**. There is no extra length prefix in
the body — the CoAP layer already gives a length. There is **no AAD**
(`b""` is passed for AAD on every encrypt and decrypt
— `connection.py:95, 100, 106`).

### Nonce derivation — directional, 64-bit LE counter, 4-byte zero pad

```python
# connection.py:95-108
def decrypt(self, enc_data):
    return self.recv_ctx.decrypt(struct.pack("=4xQ", self.recv_ctr), enc_data, b"")
def encrypt(self, dec_data):
    return self.send_ctx.encrypt(struct.pack("=4xQ", self.send_ctr), dec_data, b"")
```

Nonce = 4 bytes of zero || 8-byte little-endian counter, total 12 bytes
(ChaCha20Poly1305's IETF nonce). `=4xQ` means native size, no
alignment, 4 pad bytes, then `Q`= unsigned little-endian uint64 (the
`=` prefix forces standard sizes and **little-endian-on-this-platform**;
on `=`, Q is little-endian on x86/ARM as Python uses native, but
because `=` disables alignment but keeps native byte-order, a clean
re-implementation should hard-code little-endian). **Use little-endian.**

### Three independent counters / contexts

`EncryptionContext` keeps **send_ctr, recv_ctr, event_ctr** all
starting at 0 and incremented per direction
(`connection.py:74-87, 96, 101, 107`). The strategy is **directional**
(asymmetric): the same counter value can be used in send and recv
because the *keys* differ (recv_ctx and send_ctx wrap different HKDF
outputs — `Control-Read-Encryption-Key` vs `Control-Write-Encryption-Key`).
Events (server-pushed PUTs) use a third independent key+counter pair.

### Tag placement

`cryptography.ChaCha20Poly1305.encrypt` returns ciphertext concatenated
with the 16-byte Poly1305 tag at the end. So on the wire:

```
[ ciphertext (n bytes) ][ tag (16 bytes) ]
```

`decrypt` consumes the same layout. CoAP message body length =
plaintext length + 16.

### Resync hack

`_decrypt_response` (`connection.py:110-151`) tries on InvalidTag:
rewind recv_ctr by up to 5, then forward by up to 5, then reset both
counters to 0. If none works, it shuts down the CoAP context and
raises. **Do not copy this verbatim** — it papers over packet loss /
reordering on lossy Thread links and is explicitly noted as flailing
("self-destructing in 3, 2, 1..." comment, `connection.py:145`). If your
implementation has visibility into CoAP retransmission acks you can do
better.

---

## 4. Read/write characteristics — binary PDU, NOT JSON

CoAP uses the **same binary HAP-PDU layout HAP-BLE uses**, not the
JSON-over-HTTP shape that HAP-IP uses. The encrypted CoAP body is one or
more concatenated PDUs.

### Request PDU (`pdu.py:53-55`)

```python
buf = struct.pack("<BBBHH", 0b00000000, opcode.value, tid, iid, len(data))
return bytes(buf + data)
# Control(1) | Opcode(1) | TID(1) | IID(2 LE) | Len(2 LE) | Body(Len)
```

Note **IID is 2 bytes, body length is 2 bytes** — both little-endian.
HAP-BLE-spec PDUs use the same layout but some BLE implementations
truncate the IID to 16 bits anyway. Body length being 16 bits (not 8)
is the relevant CoAP-vs-stock-BLE detail.

### Response PDU (`pdu.py:74-100`)

```python
control, tid, status, body_len = struct.unpack("<BBBH", data[0:5])
# Control(1) | TID(1) | Status(1) | Len(2 LE) | Body(Len)
# Note: response has NO IID echo, and body_len is 2 bytes not 4
```

Response control byte must satisfy `control & 0x0E == 0x02` else marked
`BAD_CONTROL` (`pdu.py:96`). TID must echo the request TID
(`pdu.py:88`).

### Opcodes (`pdu.py:28-37`)

```
0x01 CHAR_SIG_READ      0x06 SERV_SIG_READ
0x02 CHAR_WRITE         0x09 UNK_09_READ_GATT       # accessory database
0x03 CHAR_READ          0x0B UNK_0B_SUBSCRIBE
0x04 CHAR_TIMED_WRITE   0x0C UNK_0C_UNSUBSCRIBE
0x05 CHAR_EXEC_WRITE
```

`0x09` (read accessory database) is iid=`0x0000` and returns the full
TLV-encoded `Pdu09Database` (`connection.py:395`, `structs.py:352-375`).

### Statuses (`pdu.py:40-50`)

`0=SUCCESS, 1=Unsupported PDU, 2=Max procedures, 3=Insufficient
authorization, 4=Invalid instance ID, 5=Insufficient authentication,
6=Invalid request`. Custom 256/257 are aiohomekit-internal.

### Batched (multi-PDU) requests

`encode_all_pdus` packs multiple request PDUs back-to-back in one
encrypted CoAP body, using `tid = idx` (0,1,2,...) sequentially
(`pdu.py:58-71`). `decode_all_pdus` walks them by length
(`pdu.py:103-116`). Used for reading or writing many characteristics in
a single CoAP request (`connection.py:418, 496, 549, 581, 612`).

### Read body shape

Read request body is empty (`b""`). Read response body, when present,
is a TLV with one entry `kTLVHAPParamValue` containing the raw value
bytes. `decode_pdu_03` extracts it:

```python
# connection.py:60-61
def decode_pdu_03(buf):
    return bytes(dict(TLV.decode_bytes(buf)).get(HAP_TLV.kTLVHAPParamValue))
```

Raw value bytes are then unpacked according to the characteristic's
GATT presentation format byte (`structs.py:144-167`): bool=1B, uint8,
uint16 LE, uint32 LE, uint64 LE, int32 LE, float32 LE, UTF-8 string,
or hex-encoded data.

### Write body shape

Write request body is a TLV with `kTLVHAPParamValue=<raw bytes>`
(`connection.py:511-512`):

```python
value_tlv = TLV.encode_list([(HAP_TLV.kTLVHAPParamValue, value)])
```

Write response body is empty on success; PDU status carries failure.

---

## 5. Subscribe / event delivery

aiohomekit does **NOT** use CoAP observe (RFC 7641). Subscribe is a
plain encrypted PDU with opcode `0x0B` and empty body, addressed at the
characteristic IID (`connection.py:578-582`). Unsubscribe is `0x0C`.

Events arrive as **server-initiated CoAP PUT requests** to the
controller. The controller stands up a CoAP **server** during
pair-verify (`connection.py:326-327, 366`), `bind=("::", 0)` —
ephemeral port. The accessory PUTs encrypted notification frames to
that port.

```python
# connection.py:190-203, abridged
class EventResource(resource.Resource):
    async def render_put(self, request):
        payload = self.connection.enc_ctx.decrypt_event(request.payload)
        # event payload is a stream of records, see below
        return Message(code=Code.VALID)
```

### Event payload layout (decrypted; one CoAP PUT may carry many)

```python
# connection.py:208-230
offset = 0
while True:
    _, iid, body_len = struct.unpack("<BHH", payload[offset:offset+5])
    body = payload[offset+5:offset+5+body_len]
    # body is TLV with kTLVHAPParamValue=<raw>
    ...
    offset += 5 + body_len
    if offset >= len(payload): break
```

Each record: `1 byte (unused / control?) | 2 byte IID LE | 2 byte
length LE | TLV body`. Body decodes via `decode_pdu_03` — same value
TLV as a read response. Multiple records concatenated in one PUT.
**There is no AID** in the wire record; aiohomekit hard-codes `aid=1`
when surfacing the event (`connection.py:221`, with comment
`# XXX aid`). Bridges with multiple AIDs would break here — your
implementation should preserve / synthesize the AID from the iid lookup.

The encryption uses the third HKDF key
(`Event-Salt / Event-Read-Encryption-Key`) with its own monotonic
counter, identical chacha20poly1305 nonce strategy as §3
(`connection.py:99-102`).

Subscriptions are **per-iid**, not per-accessory. One PUT can carry
events for several iids batched.

Acknowledgement: handler returns `2.03 Valid` on success
(`connection.py:234`); `4.04 Not Found` on decrypt failure
(`connection.py:203`) — note the comment "XXX invalidate
subscriptions, etc" — that path is not fully implemented.

---

## 6. Subtle stuff (don't get bitten)

- **TID is random per request**, range 1..254 (`connection.py:179`).
  Batched requests use sequential indices starting at 0
  (`pdu.py:67-69`). Don't reuse 0 for non-batched; 255 is reserved by
  convention.
- **`recv_ctr` and `send_ctr` start at 0 the moment pair-verify
  finishes** — do not reset on any subsequent CoAP retransmit; the
  CoAP-layer retransmission of the same confirmable request must reuse
  the same encrypted payload (which means the same nonce). aiohomekit
  relies on `aiocoap` to cache+retransmit.
- **Endianness is little everywhere** in HAP-PDU and value packing
  (`pdu.py`, `structs.py:148-196`). Struct prefix `<` is used
  consistently. The `=4xQ` in `connection.py:95` is the only place that
  could surprise you — treat it as little-endian.
- **No fragmentation logic.** CoAP block-wise transfer (RFC 7959) is
  not invoked; the accessory is expected to keep responses inside one
  UDP datagram. The accessory database response from opcode 0x09 can
  be large; on Thread it relies on 6LoWPAN fragmentation. If you build
  your own CoAP, ensure block2 support (and check whether your peer
  emits it).
- **Pair-verify constructs a new CoAP server context for the receive
  side**, not the existing client (`connection.py:326-327`). The reason
  is that the controller must accept inbound PUTs, so it has to be a
  CoAP **endpoint** not just a client. Notify your stack accordingly.
- **404 on encrypted POST means session is gone**: handle by tearing
  down `enc_ctx` and re-running pair-verify
  (`connection.py:168-172`). Common after accessory power cycle on Thread.
- **No AAD on any frame** — passing AAD would fail the tag check.
- **`do_pair_verify` shuts down the existing connection if already
  connected** (`connection.py:321-324`). Don't double-pair-verify on a
  live session.
- **`get_accessory_info` follows up by bulk-reading every readable
  characteristic** as part of the connect sequence
  (`connection.py:404-444`). On a constrained Thread node this is a
  burst. Consider deferring.
- **The "self-destructing" counter resync** (§3) is a hack; the comment
  literally says so (`connection.py:145`). Don't ship that logic to
  production without auditing it.
- **`raw_value = b""` on power-off / no value** — characteristic value
  TLV may legitimately be empty, treat that as "no value yet" not error
  (`connection.py:213, 423, 461`).
- **Service/characteristic types are 128-bit UUIDs encoded little-endian
  as u128** in the accessory database TLV (`structs.py:31, 270`). The
  to_dict serializer formats as `f"{self.type:X}"` then a separate
  `normalize_uuid` step converts to canonical UUID
  (`pairing.py:146-149`).
- **`presentation_format` is 7 bytes**: `<BxHxxx>` => format byte,
  unused, unit u16 LE, three unused (`structs.py:92`).

Thread-specific concerns: aiohomekit does not contain Thread mesh
routing, address selection, or SRP-registration code — it just opens a
UDP socket on the IPv6 the Thread Border Router has surfaced. Border
router quality (lossy mesh, sleepy children) shows up as CoAP timeouts
and the resync hack. Counter desync after a long sleep is realistic.

---

## 7. Pairing data persistence

`CoAPPairing` is rehydrated from a dict via
`CoAPController.load_pairing` (`controller.py:21-35`). Required fields:

| Key | Notes | Source |
|---|---|---|
| `Connection` | must equal `"CoAP"` | `controller.py:22`, `discovery.py:62` |
| `AccessoryPairingID` | accessory's HomeKit ID, lowercased for dict key | `controller.py:25, 28` |
| `AccessoryIP` | string, IPv6 literal preferred (Thread) | `pairing.py:41`, `discovery.py:60` |
| `AccessoryPort` | int | `pairing.py:41`, `discovery.py:61` |
| `AccessoryLTPK` | accessory Ed25519 LTPK, hex string | `protocol/__init__.py:519` |
| `iOSPairingId` | controller's pairing ID (UUID string) | `protocol/__init__.py:531, 546` |
| `iOSDeviceLTSK` | controller Ed25519 LTSK, hex string | `protocol/__init__.py:534-537` |
| `iOSDeviceLTPK` | controller Ed25519 LTPK, hex string (commented usage) | `protocol/__init__.py:535` |

Optional but populated by aiohomekit:
- session resume material (`session_id`, `derive` callable) is computed
  in memory after each pair-verify and not persisted across process
  restarts (`protocol/__init__.py:575-580` and `get_session_keys`
  signature) — your implementation can persist it for resume
  optimization.

There is **no** separate "AccessoryLTPK + iOSDeviceLTPK pair" object;
keys are stored as flat hex strings in the same dict that holds
endpoint info. `id` (the dict key in the controller's pairings map) is
`AccessoryPairingID.lower()` (`controller.py:28`).
