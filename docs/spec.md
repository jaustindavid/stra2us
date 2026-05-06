*this is the initial prompt used with antigravity to create a lightweight
cloud-like service for very low-resource iot devices*

# **Specification: Lightweight IoT MQ/KV Service**

## **1\. System Overview**

The service provides a stateless, high-speed interface for IoT devices to exchange ephemeral messages and persist configuration data. To minimize overhead on low-power microcontrollers (e.g., Particle Photon 2, ESP32), the system utilizes **MessagePack** for serialization and **HMAC-SHA256** for request signing, avoiding the resource intensity of full TLS or JSON parsing.

## **2\. Core Components & Architecture**

### **2.1 Backend Engine**

* **Language**: Python (FastAPI) or PHP.  
* **Storage**: Redis (primary datastore) to take advantage of native TTL (Time-To-Live) and atomic list operations.  
* **Communication**: All device-facing endpoints must support MessagePack-encoded request and response bodies.

  ### **2.2 Data Structures**

* **Queues (/q/)**: FIFO (First-In, First-Out) ephemeral storage. Messages are deleted upon consumption or after a set TTL.  
* **Key-Value Store (/kv/)**: Persistent storage for device configurations, state variables, or shared data.

  ## **3\. Functional Requirements**

  ### **3.1 Device API**

* **Publish**: `POST /q/{topic}` – Accepts a MessagePack blob.  
* **Consume**: `GET /q/{topic}` – Returns and removes the oldest message in the topic.  
* **KV Read/Write**: `GET` and `POST` to `/kv/{key}` for persistent data.

  ### **3.2 Management Web Frontend**

A browser-based interface is required for administrative oversight:

* **Data Inspector**:  
  * List all active topics and their current message counts.  
  * Create or delete topics and KV pairs manually.  
  * Peek at queued messages (view hex or decoded MessagePack) without consuming them.  
* **Activity Log**:  
  * A real-time monitor of client requests.  
  * Filterable by Client ID, timestamp, or result (Success vs. Auth Failure).  
* **Access Control List (ACL)**:  
  * Define permissions per Client ID (e.g., "Device A" can only write to `topic/sensors/*`).  
  * Toggle Read/Write vs. Read-Only access for specific keys/topics.

  ## **4\. Security & Identity**

  ### **4.1 Individual Client Authentication**

* **Unique Keys**: Every Client ID must have its own unique, independent 32-byte shared secret.  
* **HMAC Signing**: Requests must include an `X-Signature` header. The signature is calculated as: `HMAC-SHA256(ClientSecret, Payload + Timestamp)`  
* **Replay Mitigation**: The server must reject any request with a timestamp differing from the server clock by more than 300 seconds.

  ### **4.2 Key Management UI**

* **Registry**: A dashboard to register new Client IDs.  
* **Generator**: A tool within the UI to generate and display new cryptographically secure secrets for manual entry into device firmware.  
* **Revocation**: Ability to instantly rotate or revoke a secret to disconnect a specific device.

  ## **5\. Client Library Requirements (C-Compatible)**

The system must include a reference implementation for the Particle/C++ ecosystem:

* **Zero-Malloc**: Use a library like `cmp` for MessagePack to ensure all operations happen in pre-allocated memory buffers.  
* **Low Footprint**: Total memory usage for the network and security stack must remain under 10KB.  
* **Signing Logic**: Integrated HMAC-SHA256 calculation compatible with the server-side implementation.

  ## **6\. Deliverables**

* **Server Source**: Complete source code for the MQ/KV service and the Web Management UI.  
* **Schema**: Redis key-naming convention and MessagePack structure documentation.  
* **Client Example**: A C++ library and sample `.ino` file demonstrating a signed "Publish" and "KV Get" operation.  Test client in python, supporting publish, follow, get, and set.
