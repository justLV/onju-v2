// M5Stack ATOM Echo - Push-to-Talk Voice Client
// Auto-starts call on boot. Button = push-to-talk (interrupts playback, records while held).
// Connects to sesame-esp32-bridge via same protocol as onjuino.

#include <WiFi.h>
#include <WiFiUdp.h>
#include <esp_mac.h>
#include <ESPmDNS.h>
#include <driver/i2s.h>
#include <Adafruit_NeoPixel.h>
#include <Preferences.h>
#include <opus.h>

#include "credentials.h"
#include "audio_compression.h"

// ATOM Echo hardware
#define BUTTON_PIN    39
#define LED_PIN       27
#define I2S_BCK       19
#define I2S_WS        33
#define I2S_DOUT      22
#define I2S_DIN       23

#define HOST_NAME     "m5-echo"

char desired_hostname[24];
#define SAMPLE_RATE   16000
#define SAMPLE_CHUNK  512

#define DEFAULT_SERVER   "default.server.com"
#define DEFAULT_VOLUME   5

Adafruit_NeoPixel led(1, LED_PIN, NEO_GRB + NEO_KHZ800);

Preferences preferences;
String wifi_ssid, wifi_password, server_hostname;
IPAddress serverIP(0, 0, 0, 0);
WiFiUDP udp;
WiFiServer tcpServer(3001);

volatile bool pttActive = false;
volatile bool isPlaying = false;
volatile bool interruptPlayback = false;
volatile uint32_t forceMicUntil = 0; // serial 'M' command sets this
uint8_t speaker_volume = DEFAULT_VOLUME;
bool ledEnabled = true;

enum I2SMode { MODE_NONE, MODE_MIC, MODE_SPEAKER };
volatile I2SMode currentMode = MODE_NONE;

// Mic buffers
int16_t micBuffer[SAMPLE_CHUNK];
uint8_t compressedMicBuffer[SAMPLE_CHUNK];
#define DC_OFFSET_ALPHA 0.001f
float running_dc = 0.0f;

// Speaker buffer (stereo-interleaved for ALL_RIGHT I2S)
int16_t spkBuffer[8192];
int bufferThreshold = 2048; // 128ms at 16kHz — more headroom against WiFi jitter
const size_t tcpBufSize = 512;
uint8_t tcpBuffer[tcpBufSize];

// Opus decoder
OpusDecoder *opus_decoder = NULL;
const int OPUS_FRAME_SIZE = 320;  // 20ms @ 16kHz
const int OPUS_MAX_PACKET = 4000;
int16_t opus_pcm_buffer[OPUS_FRAME_SIZE];
uint8_t opus_packet_buffer[OPUS_MAX_PACKET];

// Persistent audio TCP connection (bridge keeps one open per call)
WiFiClient audioClient;
volatile bool opusTaskRunning = false;

// LED state
volatile uint16_t ledLevel = 0;
volatile uint8_t ledColor[3] = {0, 0, 0};
volatile uint8_t ledFade = 5;

// ─── I2S Mode Switching ────────────────────────────────────

void initMicI2S()
{
    if (currentMode != MODE_NONE)
        i2s_driver_uninstall(I2S_NUM_0);

    i2s_config_t cfg = {};
    cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_PDM);
    cfg.sample_rate = SAMPLE_RATE * 2; // ALL_RIGHT stereo: total rate / 2 = 16kHz mono after de-interleave
    cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
    cfg.channel_format = I2S_CHANNEL_FMT_ALL_RIGHT;
    cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
    cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
    cfg.dma_buf_count = 4;
    cfg.dma_buf_len = SAMPLE_CHUNK;

    i2s_pin_config_t pins = {};
    pins.bck_io_num = I2S_PIN_NO_CHANGE;
    pins.ws_io_num = I2S_WS;
    pins.data_out_num = I2S_PIN_NO_CHANGE;
    pins.data_in_num = I2S_DIN;

    i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL);
    i2s_set_pin(I2S_NUM_0, &pins);
    currentMode = MODE_MIC;
}

void initSpeakerI2S()
{
    if (currentMode != MODE_NONE)
        i2s_driver_uninstall(I2S_NUM_0);

    i2s_config_t cfg = {};
    cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX);
    cfg.sample_rate = SAMPLE_RATE;
    cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
    cfg.channel_format = I2S_CHANNEL_FMT_ALL_RIGHT;
    cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
    cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
    cfg.dma_buf_count = 8;
    cfg.dma_buf_len = SAMPLE_CHUNK;

    i2s_pin_config_t pins = {};
    pins.bck_io_num = I2S_BCK;
    pins.ws_io_num = I2S_WS;
    pins.data_out_num = I2S_DOUT;
    pins.data_in_num = I2S_PIN_NO_CHANGE;

    i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL);
    i2s_set_pin(I2S_NUM_0, &pins);
    currentMode = MODE_SPEAKER;
}

// Apply volume to a 16-bit sample. volume range 0-20, default 13.
inline int16_t applyVolume(int16_t sample)
{
    int32_t s = ((int32_t)sample * speaker_volume) >> 4; // /16, so vol 13 ≈ 0.8x, vol 16 = unity
    if (s > 32767) s = 32767;
    if (s < -32768) s = -32768;
    return (int16_t)s;
}

// ─── LED ────────────────────────────────────────────────────

void setLed(uint8_t r, uint8_t g, uint8_t b, uint8_t level, uint8_t fade)
{
    if (!ledEnabled) return;
    ledColor[0] = r;
    ledColor[1] = g;
    ledColor[2] = b;
    ledLevel = level;
    ledFade = fade;
}

void updateLedTask(void *param)
{
    TickType_t xLastWake = xTaskGetTickCount();
    while (1)
    {
        vTaskDelayUntil(&xLastWake, pdMS_TO_TICKS(25));
        if (ledLevel > 0)
        {
            ledLevel = (ledLevel > ledFade) ? ledLevel - ledFade : 0;
            uint8_t l = ledLevel;
            led.setPixelColor(0,
                              ledColor[0] * l / 255,
                              ledColor[1] * l / 255,
                              ledColor[2] * l / 255);
            led.show();
        }
    }
}

// ─── Config ─────────────────────────────────────────────────

void loadConfig()
{
    preferences.begin("m5echo-cfg", true);
    wifi_ssid = preferences.getString("wifi_ssid", WIFI_SSID);
    wifi_password = preferences.getString("wifi_pass", WIFI_PASSWORD);
    server_hostname = preferences.getString("server", DEFAULT_SERVER);
    speaker_volume = preferences.getUChar("volume", DEFAULT_VOLUME);
    if (speaker_volume > DEFAULT_VOLUME) speaker_volume = DEFAULT_VOLUME;
    ledEnabled = preferences.getBool("led", true);
    uint32_t savedIP = preferences.getUInt("server_ip", 0);
    preferences.end();

    if (savedIP != 0)
        serverIP = IPAddress(savedIP);

    Serial.printf("SSID: %s\nServer: %s\nVolume: %d\nLED: %s\nSaved server IP: %s\n",
                  wifi_ssid.c_str(), server_hostname.c_str(), speaker_volume,
                  ledEnabled ? "on" : "off", serverIP.toString().c_str());
}

void saveConfig()
{
    preferences.begin("m5echo-cfg", false);
    preferences.putString("wifi_ssid", wifi_ssid);
    preferences.putString("wifi_pass", wifi_password);
    preferences.putString("server", server_hostname);
    preferences.putUChar("volume", speaker_volume);
    preferences.end();
}

void enterConfigMode()
{
    Serial.println("Config mode. Commands: ssid, pass, server, volume, exit, cancel");
    while (true)
    {
        if (!Serial.available()) continue;
        String cmd = Serial.readStringUntil('\n');
        cmd.trim();

        if (cmd == "exit")          { saveConfig(); Serial.println("Saved. Restarting..."); delay(500); ESP.restart(); }
        else if (cmd == "cancel")   { Serial.println("Cancelled. Restarting..."); delay(500); ESP.restart(); }
        else if (cmd.startsWith("ssid "))   { wifi_ssid = cmd.substring(5); Serial.println("SSID: " + wifi_ssid); }
        else if (cmd.startsWith("pass "))   { wifi_password = cmd.substring(5); Serial.println("Password updated"); }
        else if (cmd.startsWith("server ")) { server_hostname = cmd.substring(7); Serial.println("Server: " + server_hostname); }
        else if (cmd.startsWith("volume ")) { speaker_volume = constrain(cmd.substring(7).toInt(), 0, 20); Serial.printf("Volume: %d\n", speaker_volume); }
        else Serial.println("Unknown command");
    }
}

// ─── Opus Decode Task ───────────────────────────────────────

void opusDecodeTask(void *param)
{
    Serial.println("Opus decode task started");

    if (!audioClient.connected() || opus_decoder == NULL)
    {
        Serial.println("ERROR: Invalid client or decoder");
        opusTaskRunning = false;
        vTaskDelete(NULL);
        return;
    }

    bool initialBufferFilled = false;
    size_t totalSamples = 0;
    uint32_t tic = millis();
    int frameCount = 0;

    while (audioClient.connected() || audioClient.available())
    {
        // During PTT: read and discard frames, don't play
        if (pttActive)
        {
            if (isPlaying)
            {
                i2s_zero_dma_buffer(I2S_NUM_0);
                isPlaying = false;
                Serial.println("Opus: paused for PTT");
            }
            // Discard any incoming frames to keep TCP flowing
            if (audioClient.available() >= 2)
            {
                uint8_t lb[2];
                audioClient.read(lb, 2);
                uint16_t fl = (lb[0] << 8) | lb[1];
                if (fl > 0 && fl <= OPUS_MAX_PACKET)
                {
                    size_t d = 0;
                    while (d < fl && (audioClient.connected() || audioClient.available()))
                    {
                        int a = audioClient.available();
                        if (a > 0) {
                            uint8_t dummy[256];
                            d += audioClient.read(dummy, min(a, min((int)(fl - d), 256)));
                        } else delay(1);
                    }
                }
            }
            else vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        // Resuming after PTT
        if (!isPlaying)
        {
            // Wait for micTask to finish switching to speaker mode
            while (currentMode != MODE_SPEAKER) vTaskDelay(1);
            isPlaying = true;
            initialBufferFilled = false;
            totalSamples = 0;
            Serial.println("Opus: resumed playback");
        }

        if (audioClient.available() < 2)
        {
            if (!audioClient.connected()) break;
            delay(1);
            continue;
        }

        // Read 2-byte frame length (big-endian)
        uint8_t len_bytes[2];
        audioClient.read(len_bytes, 2);
        uint16_t frame_len = (len_bytes[0] << 8) | len_bytes[1];

        if (frameCount < 10)
            Serial.printf("Opus frame %d: len=%d\n", frameCount, frame_len);
        frameCount++;

        // frame_len == 0 is the end-of-speech signal from the bridge
        if (frame_len == 0)
        {
            Serial.println("End of speech marker received");
            break;
        }
        if (frame_len > OPUS_MAX_PACKET)
        {
            Serial.printf("Invalid Opus frame length: %d (0x%02X 0x%02X)\n",
                          frame_len, len_bytes[0], len_bytes[1]);
            break;
        }

        // Read Opus frame data
        size_t bytes_read = 0;
        while (bytes_read < frame_len && (audioClient.connected() || audioClient.available()))
        {
            int avail = audioClient.available();
            if (avail > 0)
            {
                int to_read = min(avail, (int)(frame_len - bytes_read));
                bytes_read += audioClient.read(opus_packet_buffer + bytes_read, to_read);
            }
            else delay(1);
        }
        if (bytes_read < frame_len) break;  // incomplete frame after disconnect

        // Decode
        int num_samples = opus_decode(opus_decoder, opus_packet_buffer, frame_len,
                                      opus_pcm_buffer, OPUS_FRAME_SIZE, 0);
        if (num_samples < 0)
        {
            Serial.printf("Opus decode error: %d\n", num_samples);
            continue;
        }

        if (frameCount <= 20 && frame_len > 10)
        {
            int16_t mn = 32767, mx = -32768;
            for (int i = 0; i < num_samples; i++) {
                if (opus_pcm_buffer[i] < mn) mn = opus_pcm_buffer[i];
                if (opus_pcm_buffer[i] > mx) mx = opus_pcm_buffer[i];
            }
            Serial.printf("Opus decoded: %d samples, range [%d, %d], vol=%d\n", num_samples, mn, mx, speaker_volume);
        }

        for (int i = 0; i < num_samples; i++)
        {
            int16_t s = applyVolume(opus_pcm_buffer[i]);
            spkBuffer[totalSamples++] = s; // L
            spkBuffer[totalSamples++] = s; // R
        }

        int writeThreshold = initialBufferFilled ? 640 : bufferThreshold * 2;
        if (totalSamples >= writeThreshold)
        {
            if (!initialBufferFilled)
            {
                Serial.printf("Buffer filled: %d samples\n", totalSamples);
                initialBufferFilled = true;
            }

            size_t written = 0;
            i2s_write(I2S_NUM_0, (uint8_t *)spkBuffer, totalSamples * sizeof(int16_t), &written, portMAX_DELAY);

            if (ledEnabled && millis() - tic > 30)
            {
                tic = millis();
                uint32_t sum = 0;
                for (int i = 0; i < min((int)totalSamples, 128); i += 4)
                    sum += abs(spkBuffer[i]);
                uint8_t level = sum >> 10;
                if (level > ledLevel) ledLevel = level;
            }
            totalSamples = 0;
        }
    }

    i2s_zero_dma_buffer(I2S_NUM_0);
    isPlaying = false;
    Serial.println("Opus: connection closed");
    opusTaskRunning = false;
    vTaskDelete(NULL);
}

// ─── Mic Task ───────────────────────────────────────────────

void micTask(void *param)
{
    bool prevButton = false;

    while (1)
    {
        bool buttonDown = !digitalRead(BUTTON_PIN);

        // Button just pressed
        if (buttonDown && !prevButton)
        {
            Serial.println("PTT pressed");
            pttActive = true;
            // Wait for Opus task to stop using I2S (it checks pttActive)
            while (isPlaying) vTaskDelay(1);
            initMicI2S();
            setLed(0, 255, 50, 200, 10);
        }

        // Button just released
        if (!buttonDown && prevButton)
        {
            Serial.println("PTT released");
            pttActive = false;
            initSpeakerI2S();
            ledLevel = 0;
        }

        prevButton = buttonDown;

        bool micActive = pttActive || (forceMicUntil > millis());

        // Handle force-mic I2S mode transitions
        if (micActive && !pttActive && currentMode != MODE_MIC)
        {
            if (isPlaying) { interruptPlayback = true; while (isPlaying) vTaskDelay(1); }
            initMicI2S();
        }
        if (!micActive && !pttActive && currentMode == MODE_MIC)
        {
            initSpeakerI2S();
            Serial.println("Force mic off");
        }

        if (micActive && serverIP != IPAddress(0, 0, 0, 0))
        {
            // Read stereo-interleaved PDM data (ALL_RIGHT duplicates each sample)
            int16_t rawBuf[SAMPLE_CHUNK * 2]; // stereo pairs
            size_t bytesRead = 0;
            uint32_t t0 = millis();
            i2s_read(I2S_NUM_0, rawBuf, sizeof(rawBuf), &bytesRead, portMAX_DELAY);
            uint32_t elapsed = millis() - t0;

            // De-interleave: take every other sample (skip duplicates)
            int samplesRead = bytesRead / (2 * sizeof(int16_t)); // stereo frames
            if (samplesRead > SAMPLE_CHUNK) samplesRead = SAMPLE_CHUNK;
            for (int i = 0; i < samplesRead; i++)
                micBuffer[i] = rawBuf[i * 2];

            static int micPktCount = 0;
            if (micPktCount < 5)
                Serial.printf("Mic: read %dB in %dms -> %d mono samples\n", bytesRead, elapsed, samplesRead);
            micPktCount++;

            // DC offset removal (IIR) + gain
            for (int i = 0; i < samplesRead; i++)
            {
                running_dc += DC_OFFSET_ALPHA * (micBuffer[i] - running_dc);
                int32_t s = ((int32_t)(micBuffer[i] - (int16_t)running_dc)) * 8; // 8x gain for SPM1423
                micBuffer[i] = (int16_t)constrain(s, -32768, 32767);
            }

            encode_ulaw(micBuffer, compressedMicBuffer, samplesRead);
            udp.beginPacket(serverIP, 3000);
            udp.write(compressedMicBuffer, samplesRead);
            udp.endPacket();
        }
        else
        {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
    }
}

// ─── Setup ──────────────────────────────────────────────────

void setup()
{
    Serial.begin(115200);
    delay(500);

    led.begin();
    led.setPixelColor(0, 50, 50, 50);
    led.show();

    pinMode(BUTTON_PIN, INPUT);

    loadConfig();

    // Build hostname from prefix + last 3 bytes of MAC: e.g. "m5_echo_A1B2C3"
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(desired_hostname, sizeof(desired_hostname), "%s-%02X%02X%02X",
             HOST_NAME, mac[3], mac[4], mac[5]);

    WiFi.setHostname(desired_hostname);
    Serial.print("Hostname: ");
    Serial.println(desired_hostname);

    WiFi.begin(wifi_ssid.c_str(), wifi_password.c_str());
    Serial.print("WiFi");
    while (WiFi.status() != WL_CONNECTED)
    {
        delay(300);
        Serial.print(".");
        if (Serial.available()) {
            char ch = Serial.read();
            if (ch == 'r') ESP.restart();
            if (ch == 'c') { Serial.println(); enterConfigMode(); }
        }
    }
    Serial.println();
    Serial.print("IP: ");
    Serial.println(WiFi.localIP());
    setLed(0, 255, 50, 255, 10);

    // Network services
    udp.begin(3000);
    tcpServer.begin();
    tcpServer.setNoDelay(true);
    MDNS.begin(desired_hostname);

    // Resolve server — skip if we already have a saved IP from last session
    if (serverIP == IPAddress(0, 0, 0, 0) && server_hostname != DEFAULT_SERVER)
    {
        String queryHost = server_hostname;
        if (queryHost.endsWith(".local"))
            queryHost = queryHost.substring(0, queryHost.length() - 6);
        for (int i = 0; i < 10; i++)
        {
            IPAddress resolved = MDNS.queryHost(queryHost.c_str());
            if (resolved != INADDR_NONE)
            {
                serverIP = resolved;
                Serial.printf("Resolved %s -> %s\n", server_hostname.c_str(), serverIP.toString().c_str());
                break;
            }
            delay(200);
        }
    }
    else if (serverIP != IPAddress(0, 0, 0, 0))
        Serial.printf("Using saved server IP: %s\n", serverIP.toString().c_str());

    // Multicast announcement — include "PTT" so bridge auto-starts call
    udp.beginPacket(IPAddress(239, 0, 0, 1), 12345);
    String announce = String(desired_hostname) + " m5echo PTT";
    udp.write((const uint8_t *)announce.c_str(), announce.length());
    udp.endPacket();
    Serial.println("Announced on multicast (PTT mode)");

    // Opus decoder
    int opus_error;
    opus_decoder = opus_decoder_create(SAMPLE_RATE, 1, &opus_error);
    if (opus_error != OPUS_OK)
        Serial.printf("Opus decoder create failed: %d\n", opus_error);
    else
        Serial.println("Opus decoder initialized");

    Serial.print("Device: ");
    Serial.print(WiFi.getHostname());
    Serial.print(" @ ");
    Serial.println(WiFi.localIP());

    // Start in speaker mode
    initSpeakerI2S();

    // Launch tasks
    xTaskCreatePinnedToCore(micTask, "micTask", 4096, NULL, 1, NULL, 1);
    xTaskCreatePinnedToCore(updateLedTask, "ledTask", 2048, NULL, 2, NULL, 1);

    Serial.println("Ready - push button to talk");
}

// ─── Main Loop ──────────────────────────────────────────────

void loop()
{
    // Serial commands
    if (Serial.available())
    {
        char ch = Serial.read();
        switch (ch)
        {
        case 'r': ESP.restart(); break;
        case 'c': enterConfigMode(); break;
        case 'M':
            forceMicUntil = millis() + 10000;
            Serial.println("Force mic on for 10s");
            break;
        case 'm':
            forceMicUntil = 0;
            Serial.println("Force mic off");
            break;
        case 'T':
        {
            // Raw mic test - read samples via micTask's I2S, print
            Serial.println("Raw mic test...");
            forceMicUntil = millis() + 5000; // keep mic on via micTask
            delay(500); // let micTask switch and PDM stabilize
            int16_t testBuf[256];
            size_t br = 0;
            i2s_read(I2S_NUM_0, testBuf, sizeof(testBuf), &br, portMAX_DELAY);
            Serial.printf("Read %d bytes. First 20 samples:\n", br);
            for (int i = 0; i < 20 && i < (br/2); i++)
                Serial.printf("%d ", testBuf[i]);
            Serial.println();
            int16_t mn = 32767, mx = -32768;
            for (int i = 0; i < br/2; i++) {
                if (testBuf[i] < mn) mn = testBuf[i];
                if (testBuf[i] > mx) mx = testBuf[i];
            }
            Serial.printf("Min: %d, Max: %d\n", mn, mx);
            forceMicUntil = 0; // micTask will switch back to speaker
            break;
        }
        case 'P':
        {
            // Local tone test — no TCP, no Opus, pure I2S output
            Serial.println("Playing local 440Hz tone for 2s...");
            if (currentMode != MODE_SPEAKER) initSpeakerI2S();
            const int dur_samples = SAMPLE_RATE * 2;
            int16_t toneBuf[640]; // stereo pairs
            for (int offset = 0; offset < dur_samples; offset += 320)
            {
                for (int i = 0; i < 320; i++)
                {
                    float t = (float)(offset + i) / SAMPLE_RATE;
                    int16_t s = (int16_t)(8000.0f * sinf(2.0f * 3.14159f * 440.0f * t));
                    toneBuf[i * 2] = s;
                    toneBuf[i * 2 + 1] = s;
                }
                size_t w = 0;
                i2s_write(I2S_NUM_0, toneBuf, sizeof(toneBuf), &w, portMAX_DELAY);
            }
            i2s_zero_dma_buffer(I2S_NUM_0);
            Serial.println("Tone done");
            break;
        }
        case '+':
        case '=':
            speaker_volume = min(20, speaker_volume + 1);
            Serial.printf("Volume: %d\n", speaker_volume);
            preferences.begin("m5echo-cfg", false);
            preferences.putUChar("volume", speaker_volume);
            preferences.end();
            break;
        case '-':
            speaker_volume = max(0, speaker_volume - 1);
            Serial.printf("Volume: %d\n", speaker_volume);
            preferences.begin("m5echo-cfg", false);
            preferences.putUChar("volume", speaker_volume);
            preferences.end();
            break;
        case 'L':
            ledEnabled = !ledEnabled;
            if (!ledEnabled) { ledLevel = 0; led.setPixelColor(0, 0, 0, 0); led.show(); }
            Serial.printf("LED: %s\n", ledEnabled ? "on" : "off");
            preferences.begin("m5echo-cfg", false);
            preferences.putBool("led", ledEnabled);
            preferences.end();
            break;
        case 'A':
        {
            udp.beginPacket(IPAddress(239, 0, 0, 1), 12345);
            String a = String(desired_hostname) + " m5echo PTT";
            udp.write((const uint8_t *)a.c_str(), a.length());
            udp.endPacket();
            Serial.println("Announced (PTT)");
            break;
        }
        }
    }

    // Don't handle TCP while PTT is active
    if (pttActive)
    {
        delay(10);
        return;
    }

    WiFiClient client = tcpServer.available();
    if (!client)
    {
        delay(10);
        return;
    }

    Serial.println("TCP from " + client.remoteIP().toString());
    if (serverIP != client.remoteIP())
    {
        serverIP = client.remoteIP();
        preferences.begin("m5echo-cfg", false);
        preferences.putUInt("server_ip", (uint32_t)serverIP);
        preferences.end();
        Serial.println("Server IP saved to NVS");
    }

    // Wait for header with timeout and PTT check
    uint32_t waitStart = millis();
    while (client.available() < 6)
    {
        if (pttActive || (millis() - waitStart) > 2000)
        {
            Serial.println("Header wait aborted");
            client.stop();
            return;
        }
        delay(1);
    }

    uint8_t header[6];
    client.read(header, 6);

    // 0xAA = audio playback (persistent connection for entire call)
    if (header[0] == 0xAA)
    {
        speaker_volume = constrain(header[3], 0, 20);
        uint8_t compression_type = header[5];
        setLed(255, 255, 255, 0, header[4]);

        Serial.printf("Audio: compression=%d vol=%d\n", compression_type, speaker_volume);

        if (currentMode != MODE_SPEAKER) initSpeakerI2S();

        isPlaying = true;
        interruptPlayback = false;

        // Opus compressed audio (compression_type == 2)
        if (compression_type == 2 && opus_decoder != NULL)
        {
            // Store client globally — connection persists for entire call
            audioClient = client;
            opusTaskRunning = true;

            xTaskCreatePinnedToCore(opusDecodeTask, "opusDec", 32768, NULL, 1, NULL, 1);

            // Wait for task to finish, but keep processing serial commands
            while (opusTaskRunning)
            {
                if (Serial.available())
                {
                    char ch = Serial.read();
                    if (ch == '+' || ch == '=') {
                        speaker_volume = min(20, speaker_volume + 1);
                        Serial.printf("Volume: %d\n", speaker_volume);
                    } else if (ch == '-') {
                        speaker_volume = max(0, speaker_volume - 1);
                        Serial.printf("Volume: %d\n", speaker_volume);
                    } else if (ch == 'L') {
                        ledEnabled = !ledEnabled;
                        if (!ledEnabled) { ledLevel = 0; led.setPixelColor(0, 0, 0, 0); led.show(); }
                        Serial.printf("LED: %s\n", ledEnabled ? "on" : "off");
                        preferences.begin("m5echo-cfg", false);
                        preferences.putBool("led", ledEnabled);
                        preferences.end();
                    } else if (ch == 'r') ESP.restart();
                }
                delay(50);
            }
        }
        // PCM audio (compression_type == 0)
        else
        {
            bool initialBufferFilled = false;
            size_t totalSamples = 0;
            uint32_t tic = millis();

            while (client.connected() || client.available())
            {
                if (interruptPlayback) break;

                size_t avail = client.available();
                if (avail < 2) {
                    if (!client.connected()) break;
                    delay(2);
                    continue;
                }

                size_t toRead = min((avail / 2) * 2, tcpBufSize);
                size_t bytesRead = client.read(tcpBuffer, toRead);

                for (size_t i = 0; i < bytesRead; i += 2)
                {
                    int16_t sample16 = applyVolume((int16_t)((tcpBuffer[i + 1] << 8) | tcpBuffer[i]));
                    spkBuffer[totalSamples++] = sample16; // L
                    spkBuffer[totalSamples++] = sample16; // R
                }

                if (initialBufferFilled || totalSamples >= bufferThreshold * 2)
                {
                    if (!initialBufferFilled)
                    {
                        initialBufferFilled = true;
                        Serial.printf("Buffer filled: %d samples\n", totalSamples);
                    }

                    size_t written = 0;
                    i2s_write(I2S_NUM_0, (uint8_t *)spkBuffer, totalSamples * sizeof(int16_t), &written, portMAX_DELAY);

                    if (millis() - tic > 30)
                    {
                        tic = millis();
                        uint32_t sum = 0;
                        for (int i = 0; i < min((int)totalSamples, 128); i += 4)
                            sum += abs(spkBuffer[i]);
                        uint8_t level = sum >> 10;
                        if (level > ledLevel) ledLevel = level;
                    }
                    totalSamples = 0;
                }
            }

            if (interruptPlayback)
            {
                i2s_zero_dma_buffer(I2S_NUM_0);
                uint32_t drainStart = millis();
                while (client.connected() && (millis() - drainStart) < 1000)
                {
                    if (client.available() > 0)
                    {
                        uint8_t dummy[512];
                        client.read(dummy, min(client.available(), 512));
                    }
                    else delay(10);
                }
                interruptPlayback = false;
            }
            else
            {
                int16_t silence[240] = {};
                for (int i = 0; i < 8; i++)
                {
                    size_t w = 0;
                    i2s_write(I2S_NUM_0, silence, sizeof(silence), &w, portMAX_DELAY);
                }
            }
        }

        isPlaying = false;
        Serial.println("Playback done");
    }
    // 0xBB = set LED
    else if (header[0] == 0xBB)
    {
        setLed(0, 0, 0, 0, 0);
        led.setPixelColor(0, header[2], header[3], header[4]);
        led.show();
        client.stop();
    }
    // 0xCC = LED blink (VAD visualization)
    else if (header[0] == 0xCC)
    {
        setLed(header[2], header[3], header[4], header[1], header[5]);
        client.stop();
    }
    // 0xDD = mic timeout (accept for compatibility)
    else if (header[0] == 0xDD)
    {
        setLed(0, 255, 50, 100, 5);
        client.stop();
    }
    else
    {
        Serial.printf("Unknown command: 0x%02X\n", header[0]);
        client.stop();
    }
}
