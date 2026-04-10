#include <WiFi.h>
#include <WiFiUdp.h>
#include <ESPmDNS.h>
#include <driver/i2s.h>
#include <Adafruit_NeoPixel.h>
#include <Preferences.h>
#include <opus.h>

#if __has_include("git_hash.h") // optionally setup post-commit hook to generate git_hash.h
#include "git_hash.h"
#else
#define GIT_HASH "------"
#endif

#define BOARD_V3
#define HOST_NAME "onju"

// Set to true for push-to-talk mode (hold button = mic on, release = mic off)
// PTT devices auto-start a call on boot and power-off to disconnect
#define PTT_MODE false

#include "custom_boards.h"
#include "credentials.h"
#include "audio_compression.h"

#define TOUCH_EN
#define DISABLE_HARDWARE_MUTE  // Temporary: disable mute switch check

// Wi-Fi settings - edit these in credentials.h
Preferences preferences;

// Define default values
#define DEFAULT_SERVER_HOSTNAME "default.server.com"  // Set via serial config: `c` then `server justins-mac-mini.local`
#define DEFAULT_MIC_TIMEOUT 30000
#define DEFAULT_SPEAKER_VOLUME 14

// Configuration variables
String wifi_ssid;
String wifi_password;
String server_hostname;
int mic_timeout_default;
uint8_t speaker_volume;

Adafruit_NeoPixel leds(LED_COUNT, LED_PIN, NEO_GRB + NEO_KHZ800);

// UDP Settings
IPAddress serverIP(0, 0, 0, 0); // Placeholder until we get first TCP client greeting us
unsigned int udpPort = 3000;
WiFiUDP udp;

// TCP Settings
WiFiServer tcpServer(3001);

volatile bool isPlaying = false;
uint32_t mic_timeout = 0;

// LED globals that are set then ramped down by updateLedTask to create pulse effect
volatile uint16_t ledLevel = 0;
volatile uint8_t ledColor[3] = {0, 0, 0};
volatile uint8_t ledFade = 5;

// Touch debounce timing
volatile unsigned long lastTouchTimeLeft = 0;
volatile unsigned long lastTouchTimeCenter = 0;
volatile unsigned long lastTouchTimeRight = 0;
const unsigned long TOUCH_DEBOUNCE_MS = 800; // 800ms between valid touches
const unsigned long CENTER_DEBOUNCE_MS = 150; // shorter for center so double-tap window has room

// Double-tap detection: requires a prior completed normal tap before arming.
// Why: prevents accidental disable from cold start or back-to-back double-taps.
volatile unsigned long lastTapTime = 0;
volatile bool tapPendingArm = false;
volatile bool doubleTapArmed = false;
const unsigned long DOUBLE_TAP_WINDOW_MS = 700;
const unsigned long MIC_LISTEN_MS = 20000; // 20s default (server VAD extends when needed)

// Device enable state — toggled by double-tap, gates mic + audio playback
volatile bool deviceEnabled = true;

// PTT state: mic only active while button held
volatile bool pttHeld = false;

const double gammaValue = 1.8; // dropped this down from typical 2.2 to avoid flicker
uint8_t gammaCorrectionTable[256];

// Speaker buffer settings
const size_t tcpBufferSize = 512; // for received audio data before processing into 32-bit chunks for MAX98357A
uint8_t tcpBuffer[tcpBufferSize];

int32_t *wavData = NULL; // assign later as PSRAM (or not) as a buffer for playback from TCP

// how many samples to load from TCP before starting playing (avoid jitter due to running out of data w/ bad wifi)
// With Opus compression, we can use smaller buffers (less latency)
#ifdef USE_PSRAM
int bufferThreshold = 4096;  // 256ms @ 16kHz (12.8 frames @ 20ms)
#else
int bufferThreshold = 1024;  // 64ms @ 16kHz (3.2 frames @ 20ms)
#endif

// Mic settings
#define SAMPLE_CHUNK_SIZE 512                  // 32ms at 16kHz for Silero VAD, fits in UDP packet (512 bytes μ-law < 1400)
int32_t micBuffer[SAMPLE_CHUNK_SIZE];          // For raw values from I2S
int16_t convertedMicBuffer[SAMPLE_CHUNK_SIZE]; // For converted values to be sent over UDP

#define MAX_ALLOWED_OFFSET 16000
#define MIC_OFFSET_AVERAGING_FRAMES 1
#define DC_OFFSET_ALPHA 0.001f // IIR filter coefficient for DC offset tracking
#define VAD_MIC_EXTEND 5000 // ensure there's always another 5s after last VAD detected by server to avoid cutting off while talking

// Audio compression
#define USE_COMPRESSION true        // Enable μ-law compression (2x bandwidth reduction)

bool mute = false; // track state of mute button

uint8_t compressedMicBuffer[SAMPLE_CHUNK_SIZE]; // For μ-law compressed audio

// Opus decoder
OpusDecoder *opus_decoder = NULL;
const int OPUS_FRAME_SIZE = 320;  // 20ms @ 16kHz
const int OPUS_MAX_PACKET = 4000; // Max Opus packet size
int16_t opus_pcm_buffer[OPUS_FRAME_SIZE];
uint8_t opus_packet_buffer[OPUS_MAX_PACKET]; // Global buffer to avoid stack overflow

// Global client pointer for opus task
WiFiClient *opusClient = NULL;
volatile bool opusTaskRunning = false;
volatile bool interruptPlayback = false; // Flag to interrupt audio playback

i2s_config_t i2s_config = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX | I2S_MODE_RX),
    .sample_rate = 16000,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 4,
    .dma_buf_len = SAMPLE_CHUNK_SIZE}; // mostly set by needs of microphone

i2s_pin_config_t pin_config = {
    .bck_io_num = I2S_BCK_PIN,
    .ws_io_num = I2S_WS_PIN,
    .data_out_num = I2S_OUT,
    .data_in_num = I2S_IN};

void setup()
{
    Serial.begin(115200);

    leds.begin();
    leds.show();

    for (int i = 0; i < LED_COUNT; i++)
    {
        leds.setPixelColor(i, 50, 50, 50);
    }
    leds.show();
    delay(500); // give time for Serial to begin
    leds.clear();
    leds.show();

    Serial.println("Gamma LUT:");
    for (int i = 0; i < 256; i++)
    {
        double value = static_cast<double>(i) / 255.0;
        gammaCorrectionTable[i] = static_cast<uint8_t>(pow(value, gammaValue) * 255.0 + 0.5);
        Serial.print(gammaCorrectionTable[i]);
        Serial.print(" ");
    }

    Serial.println();

    Serial.println("Board version: " + String(BOARD_NAME));
    Serial.println("Git hash:" + String(GIT_HASH));

    pinMode(MUTE, INPUT_PULLUP);

#ifdef SPEAKER_EN
    Serial.println("Setting SPEAKER_EN");
    pinMode(SPEAKER_EN, OUTPUT);
    digitalWrite(SPEAKER_EN, HIGH);
#endif

#ifdef TOUCH_EN
    touchAttachInterrupt(T_L, gotTouch1, 1250); // tweak these as needed, probably also needs some debounce from experience
    touchAttachInterrupt(T_C, gotTouch2, 1800);
    touchAttachInterrupt(T_R, gotTouch3, 1250);
    Serial.println("Touch enabled");
#endif

    // Build hostname from prefix + last 3 bytes of MAC: e.g. "onju-A1B2C3"
    uint8_t mac[6];
    WiFi.macAddress(mac);
    char desired_hostname[20];
    snprintf(desired_hostname, sizeof(desired_hostname), "%s-%02X%02X%02X", HOST_NAME, mac[3], mac[4], mac[5]);

    if (WiFi.setHostname(desired_hostname))
    {
        Serial.print("Hostname set to ");
        Serial.println(desired_hostname);
    }

    const char *hostname = WiFi.getHostname();
    if (hostname)
    {
        Serial.print("Host Name: ");
        Serial.println(hostname);
    }
    else
    {
        Serial.println("Failed to get hostname");
    }

#ifdef USE_PSRAM
    if (psramInit())
    {
        Serial.println("PSRAM initialized");
    }
    else
    {
        Serial.println("PSRAM failed to init!");
    }
#else
    Serial.println("PSRAM disabled");
#endif

    loadConfig();

    WiFi.begin(wifi_ssid.c_str(), wifi_password.c_str());

    Serial.print("Connecting to WiFi");

    int ledindex = 1;
    while (WiFi.status() != WL_CONNECTED)
    {
        delay(300);
        leds.clear();
        leds.setPixelColor(ledindex, 40, 40, 40);
        leds.show();
        ledindex = (ledindex % 4) + 1; // cycle through middle LEDs index 1-4 while connecting
        Serial.print(".");
        if (Serial.available())
        {
            char inChar = (char)Serial.read();
            if (inChar == 'r')
            {
                Serial.println("[UART] Reset command from UART");
                esp_restart();
            }
        }
    }
    Serial.println(" Connected to WiFi");
    Serial.println(WiFi.localIP());

    leds.clear();
    leds.show();

    setLed(0, 255, 50, 255, 10); // green pulse

    Serial.println("Starting UDP");
    udp.begin(udpPort);

    Serial.println("Starting TCP server");
    tcpServer.begin();

    // mDNS: register ourselves and try to resolve server hostname
    MDNS.begin(desired_hostname);

    if (serverIP != IPAddress(0, 0, 0, 0))
    {
        Serial.printf("Using saved server IP: %s\n", serverIP.toString().c_str());
    }
    else if (server_hostname != DEFAULT_SERVER_HOSTNAME)
    {
        Serial.printf("Resolving server hostname: %s\n", server_hostname.c_str());
        String queryHost = server_hostname;
        if (queryHost.endsWith(".local"))
        {
            queryHost = queryHost.substring(0, queryHost.length() - 6);
        }
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
        if (serverIP == IPAddress(0, 0, 0, 0))
        {
            Serial.println("mDNS resolution failed, falling back to multicast discovery");
        }
    }

    Serial.println("Sending multicast packet to announce presence");
    udp.beginPacket(IPAddress(239, 0, 0, 1), 12345);
    String mcast_string = String(hostname) + " " + String(GIT_HASH);
    if (PTT_MODE) mcast_string += " PTT";
    udp.write(reinterpret_cast<const uint8_t *>(mcast_string.c_str()), mcast_string.length());
    udp.endPacket();

    // Device starts enabled; mic is gated by mic_timeout (0 = expired, user must tap or greeting sets it)
    deviceEnabled = true;
    if (PTT_MODE) {
        Serial.println("PTT mode: device enabled on boot");
        setLed(0, 100, 255, 200, 3); // blue pulse = PTT idle, waiting for bridge
    } else {
        Serial.println("VOX mode: device enabled, tap to start mic");
    }

    i2s_driver_install(I2S_NUM, &i2s_config, 0, NULL);
    i2s_set_pin(I2S_NUM, &pin_config);

#ifdef USE_PSRAM
    Serial.println("Allocating wavData - PSRAM");
    size_t free_psram = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    size_t total_psram = heap_caps_get_total_size(MALLOC_CAP_SPIRAM);
    Serial.println("PSRAM free: " + String(free_psram));
    Serial.println("PSRAM total: " + String(total_psram));
    Serial.println("PSRAM used: " + String(total_psram - free_psram));

    wavData = (int32_t *)ps_malloc((2 * 1024 * 1024) / sizeof(int32_t));
    if (wavData == NULL)
    {
        Serial.println("Memory allocation failed!");
        while (1)
            ;
    }
    else
    {
        Serial.println("Memory allocation successful!");
    }
    free_psram = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    Serial.println("PSRAM used: " + String(total_psram - free_psram));
#else
    Serial.println("Allocating wavData - no PSRAM");
    wavData = (int32_t *)malloc((bufferThreshold * 4));
#endif

    // Initialize Opus decoder
    int opus_error;
    opus_decoder = opus_decoder_create(16000, 1, &opus_error);  // 16kHz, mono
    if (opus_error != OPUS_OK)
    {
        Serial.printf("Opus decoder create failed: %d\n", opus_error);
    }
    else
    {
        Serial.println("Opus decoder initialized");
    }

    xTaskCreatePinnedToCore(micTask, "MicTask", 4096, NULL, 1, NULL, 1);
    xTaskCreatePinnedToCore(updateLedTask, "updateLedTask", 2048, NULL, 2, NULL, 1);
    xTaskCreatePinnedToCore(touchTask, "TouchTask", 2048, NULL, 2, NULL, 1);
}

void loop()
{
#if !defined(BOARD_V1) && !defined(DISABLE_HARDWARE_MUTE)
    if (digitalRead(MUTE) && !mute)
    {
        mute = true;
        setLed(255, 50, 0, 255, 2); // slow fade red
        mic_timeout = 0; // Turn off mic when muted
    }
    else if (!digitalRead(MUTE) && mute)
    {
        mute = false;
        setLed(0, 255, 50, 255, 10); // faster fade green
        mic_timeout = millis() + 60000; // 60s timeout when unmuted
    }
#endif

    if (Serial.available())
    {
        char inChar = (char)Serial.read();
        switch (inChar)
        {
        case 'r':
            Serial.println("[UART] Reset command from UART");
            Serial.flush();
            delay(100);
            ESP.restart();
            break;
        case 'M':
            mic_timeout = millis() + (600 * 1000);
            Serial.println("[UART] Turned on mic for 10 min");
            break;
        case 'A':
        {
            Serial.println("[UART] Sending multicast announcement");
            udp.beginPacket(IPAddress(239, 0, 0, 1), 12345);
            String mcast_string = String(WiFi.getHostname()) + " " + String(GIT_HASH);
            if (PTT_MODE) mcast_string += " PTT";
            udp.write(reinterpret_cast<const uint8_t *>(mcast_string.c_str()), mcast_string.length());
            udp.endPacket();
            Serial.println("[UART] Multicast sent");
            break;
        }
        case 'm':
            mic_timeout = 0;
            Serial.println("[UART] Turned off mic");
            break;
        case 'W':
            Serial.println("[UART] LED pulse test (fast ramp white)");
            setLed(255, 255, 255, 255, 20);
            break;
        case 'w':
            Serial.println("[UART] LED pulse test (slow ramp white)");
            setLed(255, 255, 255, 255, 1);
            break;
        case 'L':
            Serial.println("[UART] LED's all on max brightness (white)");
            setLed(0, 0, 0, 0, 0); // stop ramping down function from running
            for (int i = 0; i < 6; i++)
            {
                leds.setPixelColor(i, 255, 255, 255);
            }
            leds.show();
            break;
        case 'l':
            Serial.println("[UART] LED's all off");
            for (int i = 0; i < 6; i++)
            {
                leds.setPixelColor(i, 0, 0, 0);
            }
            leds.show();
            break;
        case 'c':
            enterConfigMode();
            break;
        default:
            Serial.println("[UART] Unknown command: " + String(inChar));
            break;
        }
    }

    WiFiClient client = tcpServer.available();
    if (client)
    {
        Serial.println("New client connection: " + client.remoteIP().toString());

        if (serverIP != client.remoteIP())
        {
            serverIP = client.remoteIP();
            preferences.begin("onjuino-config", false);
            preferences.putUInt("server_ip", (uint32_t)serverIP);
            preferences.end();
            Serial.println("Server IP saved to NVS");
        }

        // Persistent connection loop: process commands until disconnect or timeout
        while (client.connected() || client.available())
        {
            // Wait for 6-byte header (500ms timeout for responsive disconnect detection)
            uint32_t waitStart = millis();
            while (client.available() < 6)
            {
                if (!client.connected()) goto tcp_cleanup;
                if ((millis() - waitStart) > 500)
                {
                    goto tcp_cleanup;
                }
                delay(1);
            }

            uint8_t header[6];
            client.read(header, 6);

            Serial.print("Header ( ");
            for (int i = 0; i < 6; i++)
            {
                Serial.print(header[i], HEX);
                Serial.print(" ");
            }
            Serial.println(")");

            /*
            header[0]   0xAA for audio
            header[1:2] mic timeout in seconds (after audio is done playing)
            header[3]   volume
            header[4]   fade rate of LED's VAD visualization
            header[5]   compression type: 0=PCM (raw), 1=μ-law, 2=Opus
            */
            if (header[0] == 0xAA)
            {
                if (!deviceEnabled)
                {
                    Serial.println("Ignoring audio - device disabled");
                    break;
                }
                leds.clear();
                leds.show();
                uint16_t timeout = header[1] << 8 | header[2];
                speaker_volume = header[3];
                uint8_t compression_type = header[5];
                setLed(255, 255, 255, 0, header[4]);

                Serial.printf("Received audio (compression=%d) with mic timeout %d seconds, volume %d\n",
                             compression_type, timeout, speaker_volume);

                if (speaker_volume > 20)
                {
                    speaker_volume = 20;
                }

                isPlaying = true;

                bool initialBufferFilled = false;
                uint32_t tic = millis();
                size_t totalSamplesRead = 0;

                size_t bytesAvailable, bytesToRead, bytesRead, bytesWritten, bytesToWrite;
                int16_t sample16;
                uint32_t sum = 0;
                bool wasInterrupted = false;

                // Handle Opus compressed audio
                if (compression_type == 2 && opus_decoder != NULL)
                {
                    Serial.println("Starting Opus decode task with 32KB stack");
                    opusClient = &client;
                    opusTaskRunning = true;
                    interruptPlayback = false;

                    xTaskCreatePinnedToCore(
                        opusDecodeTask,
                        "OpusDecodeTask",
                        32768,
                        NULL,
                        1,
                        NULL,
                        1
                    );

                    while (opusTaskRunning)
                    {
                        delay(100);
                    }

                    wasInterrupted = interruptPlayback;

                    if (interruptPlayback)
                    {
                        Serial.println("Draining TCP buffer after interrupt...");
                        i2s_zero_dma_buffer(I2S_NUM);

                        uint32_t drainStart = millis();
                        while (client.connected() && (millis() - drainStart) < 1000)
                        {
                            if (client.available() >= 2)
                            {
                                uint8_t len_bytes[2];
                                client.read(len_bytes, 2);
                                uint16_t frame_len = (len_bytes[0] << 8) | len_bytes[1];

                                if (frame_len > 0 && frame_len <= OPUS_MAX_PACKET)
                                {
                                    size_t bytes_discarded = 0;
                                    while (bytes_discarded < frame_len && client.available() > 0)
                                    {
                                        uint8_t dummy[256];
                                        int to_read = min((int)(frame_len - bytes_discarded), 256);
                                        int read_count = client.read(dummy, to_read);
                                        if (read_count > 0)
                                        {
                                            bytes_discarded += read_count;
                                        }
                                        else
                                        {
                                            delay(1);
                                        }
                                    }
                                }
                            }
                            else
                            {
                                delay(10);
                            }
                        }

                        Serial.println("TCP drain complete");
                        interruptPlayback = false;
                    }
                    else
                    {
                        Serial.println("Opus decode task completed normally");
                    }
                }
                // Handle PCM audio (compression_type == 0)
                else
                {
                    unsigned long pcmReadStart = millis();
                    while (client.connected())
                    {
                        if (interruptPlayback)
                        {
                            Serial.println("PCM playback interrupted by user");
                            break;
                        }

                        bytesAvailable = client.available();

                        if (bytesAvailable >= 2)
                        {
                            bytesToRead = (bytesAvailable / 2) * 2;
                            if (bytesToRead > tcpBufferSize)
                            {
                                bytesToRead = tcpBufferSize;
                            }

                            bytesRead = client.read(tcpBuffer, bytesToRead);
                            pcmReadStart = millis();

                            for (size_t i = 0; i < bytesRead; i += 2)
                            {
                                sample16 = (tcpBuffer[i + 1] << 8) | tcpBuffer[i];
                                wavData[totalSamplesRead++] = (int32_t)sample16 << speaker_volume;
                            }
                            if (initialBufferFilled || totalSamplesRead >= bufferThreshold)
                            {
                                if (!initialBufferFilled)
                                {
                                    Serial.println("Initial buffer filled. totalSamplesRead: " + String(totalSamplesRead));
                                    initialBufferFilled = true;
                                }

                                bytesToWrite = totalSamplesRead * 4;
                                bytesWritten = 0;

                                i2s_write(I2S_NUM, (uint8_t *)wavData, bytesToWrite, &bytesWritten, portMAX_DELAY);

                                if (millis() - tic > 30)
                                {
                                    tic = millis();
                                    for (int i = 0; i < 128; i += 4)
                                    {
                                        sum += abs(wavData[i]);
                                    }
                                    uint8_t sum_u8 = sum >> (speaker_volume + 8);

                                    if (sum_u8 > ledLevel)
                                    {
                                        ledLevel = sum_u8;
                                    }
                                    sum = 0;
                                }
                                totalSamplesRead = 0;
                            }
                        }
                        else if (millis() - pcmReadStart > 2000)
                        {
                            // TCP froze with connection still open — force-close so we
                            // don't loop the I2S DMA buffer indefinitely.
                            Serial.println("PCM playback: TCP stalled, closing connection");
                            client.stop();
                            break;
                        }
                        else
                        {
                            delay(2);
                        }
                    }

                    wasInterrupted = interruptPlayback;

                    if (interruptPlayback)
                    {
                        Serial.println("Draining PCM TCP buffer after interrupt...");
                        i2s_zero_dma_buffer(I2S_NUM);

                        uint32_t drainStart = millis();
                        while (client.connected() && (millis() - drainStart) < 1000)
                        {
                            if (client.available() > 0)
                            {
                                uint8_t dummy[512];
                                int available = min(client.available(), 512);
                                client.read(dummy, available);
                            }
                            else
                            {
                                delay(10);
                            }
                        }

                        Serial.println("PCM TCP drain complete");
                        interruptPlayback = false;
                    }
                } // end else (PCM handling)

                // Only flush silence if not interrupted
                if (!wasInterrupted)
                {
                    uint32_t silenceBuffer[240];
                    memset(silenceBuffer, 0, sizeof(silenceBuffer));
                    for (int i = 0; i < 8; i++)
                    {
                        size_t bytesWritten = 0;
                        i2s_write(I2S_NUM, silenceBuffer, sizeof(silenceBuffer), &bytesWritten, portMAX_DELAY);
                    }
                }

                isPlaying = false;

                if (!PTT_MODE && deviceEnabled) {
                    uint32_t timeout_ms = max((uint32_t)(timeout * 1000), (uint32_t)MIC_LISTEN_MS);
                    mic_timeout = millis() + timeout_ms;
                    Serial.println("Set mic_timeout to " + String(mic_timeout) + " (" + String(timeout_ms/1000) + "s)");
                }
                Serial.println("Done loading audio in buffers in " + String(millis() - tic) + "ms");
            }
            /*
            header[0]   0xBB for set LED command
            header[1]   bitmask of which LED's to set
            header[2:4] RGB color
            */
            else if (header[0] == 0xBB)
            {
                Serial.println("Received custom LED command (0xBB)");
                setLed(0, 0, 0, 0, 0);
                uint8_t bitmask = header[1];
                for (int i = 0; i < 6; i++)
                {
                    if (bitmask & (1 << i))
                    {
                        leds.setPixelColor(i, header[2], header[3], header[4]);
                    }
                }
                leds.show();
            }
            /*
            header[0]   0xCC for LED blink command
            header[1]   starting intensity for rampdown
            header[2:4] RGB color
            header[5]   fade rate
            */
            else if (header[0] == 0xCC)
            {
                setLed(header[2], header[3], header[4], header[1], header[5]);

                // PTT doesn't use mic timeouts — skip extension logic
                if (PTT_MODE) ;
                else if(!deviceEnabled) ;
                else if(mic_timeout > millis())
                {
                    if (mic_timeout < (millis() + VAD_MIC_EXTEND))
                    {
                        mic_timeout = millis() + VAD_MIC_EXTEND;
                        Serial.println("Extended mic timeout to " + String(mic_timeout));
                    }
                }
            }
            /*
            header[0]   0xDD for mic timeout command (used by sesame-esp32-bridge)
            header[1:2] mic timeout in seconds
            header[3:5] not used
            */
            else if (header[0] == 0xDD)
            {
                Serial.println("Received mic timeout command (0xDD)");
                uint16_t timeout = header[1] << 8 | header[2];
                mic_timeout = millis() + (uint32_t)timeout * 1000;
            }
            else
            {
                Serial.println("Received unknown command");
                setLed(255, 0, 0, 255, 6);
                break; // unknown command, exit connection loop
            }
        } // end persistent connection loop

tcp_cleanup:
        client.stop();
    }
    delay(10);
}

void opusDecodeTask(void *pvParameters)
{
    Serial.println("Opus decode task started");

    WiFiClient *client = opusClient;
    if (!client || !client->connected() || opus_decoder == NULL)
    {
        Serial.println("ERROR: Invalid client or decoder in opus task");
        opusTaskRunning = false;
        vTaskDelete(NULL);
        return;
    }

    bool initialBufferFilled = false;
    size_t totalSamplesRead = 0;
    uint32_t tic = millis();
    uint32_t sum = 0;

    while (client->connected() || client->available())
    {
        // Check for user interrupt
        if (interruptPlayback)
        {
            Serial.println("Playback interrupted by user");
            break;
        }
        // Read 2-byte frame length (ensure both bytes are read)
        uint8_t len_bytes[2];
        size_t len_read = 0;
        unsigned long readStart = millis();
        while (len_read < 2)
        {
            if (interruptPlayback) break;
            if (client->available() > 0)
            {
                len_read += client->read(len_bytes + len_read, 2 - len_read);
            }
            else if (!client->connected())
            {
                break;
            }
            else if (millis() - readStart > 2000)
            {
                // TCP froze with connection still open — force-close so the outer
                // loop bails instead of looping the I2S DMA buffer indefinitely.
                Serial.println("Opus task: TCP stalled waiting for frame length, closing connection");
                client->stop();
                break;
            }
            else
            {
                delay(1);
            }
        }
        if (interruptPlayback || len_read < 2) break;
        uint16_t frame_len = (len_bytes[0] << 8) | len_bytes[1];

        // frame_len == 0 is the end-of-speech signal from the bridge
        if (frame_len == 0)
        {
            Serial.println("End of speech marker received");
            break;
        }
        if (frame_len > OPUS_MAX_PACKET)
        {
            Serial.printf("Invalid Opus frame length: %d, skipping rest of stream\n", frame_len);
            // Drain remaining TCP data to avoid corrupting next connection
            while (client->available()) client->read();
            break;
        }

        // Read Opus frame
        size_t bytes_read = 0;
        unsigned long frameReadStart = millis();
        while (bytes_read < frame_len && (client->connected() || client->available()))
        {
            if (interruptPlayback) break;
            int avail = client->available();
            if (avail > 0)
            {
                int to_read = min(avail, (int)(frame_len - bytes_read));
                bytes_read += client->read(opus_packet_buffer + bytes_read, to_read);
                frameReadStart = millis();
            }
            else if (millis() - frameReadStart > 2000)
            {
                Serial.println("Opus task: TCP stalled mid-frame, closing connection");
                client->stop();
                break;
            }
            else
            {
                delay(1);
            }
        }
        if (interruptPlayback || bytes_read < frame_len) break;  // incomplete frame, disconnect, or interrupt

        // Decode Opus frame
        int num_samples = opus_decode(opus_decoder, opus_packet_buffer, frame_len,
                                      opus_pcm_buffer, OPUS_FRAME_SIZE, 0);

        if (num_samples < 0)
        {
            Serial.printf("Opus decode error: %d\n", num_samples);
            continue;
        }

        // Convert to 32-bit and apply volume
        for (int i = 0; i < num_samples; i++)
        {
            wavData[totalSamplesRead++] = (int32_t)opus_pcm_buffer[i] << speaker_volume;
        }

        // Start draining once we have reservoir
        if (initialBufferFilled || totalSamplesRead >= bufferThreshold)
        {
            if (!initialBufferFilled)
            {
                Serial.println("Initial buffer filled. totalSamplesRead: " + String(totalSamplesRead));
                initialBufferFilled = true;
            }

            size_t bytesToWrite = totalSamplesRead * 4; // int32_t
            size_t bytesWritten = 0;
            i2s_write(I2S_NUM, (uint8_t *)wavData, bytesToWrite, &bytesWritten, portMAX_DELAY);

            if (millis() - tic > 30)
            {
                tic = millis();
                for (int i = 0; i < 128; i += 4)
                {
                    sum += abs(wavData[i]);
                }
                uint8_t sum_u8 = sum >> (speaker_volume + 8);

                if (sum_u8 > ledLevel)
                {
                    ledLevel = sum_u8;
                }
                sum = 0;
            }
            totalSamplesRead = 0;
        }
    }

    i2s_zero_dma_buffer(I2S_NUM);
    Serial.println("Opus decode task finished");
    opusTaskRunning = false;
    vTaskDelete(NULL);
}

void micTask(void *pvParameters)
{
    Serial.println("Mic task initialized, calculating initial offset... [currently not used]");

    int64_t sum = 0;
    int16_t shifted_value = 0;

    for (int i = 0; i < MIC_OFFSET_AVERAGING_FRAMES; i++)
    {
        size_t bytesRead = 0;
        i2s_read(I2S_NUM, micBuffer, sizeof(micBuffer), &bytesRead, portMAX_DELAY);
        for (int i = 0; i < sizeof(micBuffer) / sizeof(micBuffer[0]); i++)
        {
            shifted_value = static_cast<int16_t>(micBuffer[i] >> 14);
            sum += shifted_value;
        }
        Serial.println(shifted_value);
    }
    int16_t offset = sum / (sizeof(micBuffer) / sizeof(micBuffer[0])) / MIC_OFFSET_AVERAGING_FRAMES;

    Serial.println("Calculated mic offset: " + String(offset));
    if (abs(offset) > MAX_ALLOWED_OFFSET)
    {
        Serial.println("Calculated offset of is too large, using zero!");
        offset = 0;
    }

    float running_dc = (float)offset; // IIR DC offset tracker

    int counter = 0;
    bool prevState = false;

    while (1)
    {
        bool currentState = false;
        if (isPlaying || mute) // don't listen while playing audio or muted
            ;
        else if (!deviceEnabled) // device disabled by double-tap
            ;
        else if (serverIP == IPAddress(0, 0, 0, 0)) // no server greeted us yet, so nowhere to send data
            ;
        else if (PTT_MODE && !pttHeld) // PTT: only send when button held
            ;
        else if (!PTT_MODE && mic_timeout < millis()) // VOX: alotted time for speaking has passed
        {
            if (prevState)
            {
                Serial.println("Timeout reached");
            }
        }
        else
        {
            size_t bytesRead = 0;
            i2s_read(I2S_NUM, micBuffer, sizeof(micBuffer), &bytesRead, portMAX_DELAY);

            // Convert to 16-bit and remove DC offset with IIR filter
            for (int i = 0; i < SAMPLE_CHUNK_SIZE; i++)
            {
                int16_t sample = static_cast<int16_t>(micBuffer[i] >> 14);
                running_dc += DC_OFFSET_ALPHA * (sample - running_dc);
                convertedMicBuffer[i] = sample - (int16_t)running_dc;
            }

            // Transmit audio
            counter++;
            udp.beginPacket(serverIP, udpPort);

            if (USE_COMPRESSION)
            {
                encode_ulaw(convertedMicBuffer, compressedMicBuffer, SAMPLE_CHUNK_SIZE);
                udp.write(compressedMicBuffer, SAMPLE_CHUNK_SIZE); // 480 bytes instead of 960
            }
            else
            {
                udp.write((uint8_t *)convertedMicBuffer, sizeof(convertedMicBuffer));
            }

            udp.endPacket();
            currentState = true;
        }

        if (currentState != prevState)
        {
            if (currentState)
            {
                Serial.println("Started recording");
            }
            else
            {
                Serial.print("Stopped recording. Packets: ");
                Serial.println(counter);
                counter = 0;
            }
            prevState = currentState;
        }
        vTaskDelay(pdMS_TO_TICKS(1));
    }
}

void updateLedTask(void *parameter)
{
    Serial.println("Started updateLedTask");
    TickType_t xLastWakeTime;
    const TickType_t xFrequency = pdMS_TO_TICKS(25);

    xLastWakeTime = xTaskGetTickCount();

    while (1)
    {
        vTaskDelayUntil(&xLastWakeTime, xFrequency);
        if (ledLevel > 0)
        {
            if (ledLevel > ledFade)
            {
                ledLevel = ledLevel - ledFade;
            }
            else
            {
                ledLevel = 0;
            }

            for (int i = 1; i < 5; i++)
            {
                uint8_t adjustedLedLevel = ledLevel;
                if (i == 1 || i == 4) // make edges dimmer
                {
                    adjustedLedLevel >>= 1;
                }

                adjustedLedLevel = gammaCorrectionTable[adjustedLedLevel];

                leds.setPixelColor(i,
                                   ledColor[0] * adjustedLedLevel / 255,
                                   ledColor[1] * adjustedLedLevel / 255,
                                   ledColor[2] * adjustedLedLevel / 255);
            }
            leds.show();
        }
    }
}

/**
 * @brief Set the LED color, starting intensity and fade rate
 *
 * @param r Red
 * @param g Green
 * @param b Blue
 * @param level Starting intensity that the LED ramps down from
 * @param fade Rate at which the LED ramps down
 */
void setLed(uint8_t r, uint8_t g, uint8_t b, uint8_t level, uint8_t fade)
{
    ledColor[0] = r;
    ledColor[1] = g;
    ledColor[2] = b;
    ledLevel = level;
    ledFade = fade;
}

// volume currently implemented as header from server
void touchTask(void *parameter)
{
    while (1) {
        if (PTT_MODE) {
            // PTT: poll touch state to detect hold/release
            // touchRead returns LOW when touched on ESP32-S3
            uint16_t val = touchRead(T_C);
            bool touched = (val < 1800); // same threshold as ISR

            if (touched && !pttHeld) {
                pttHeld = true;
                Serial.println("PTT: button DOWN");

                // Interrupt playback if assistant is speaking.
                // Don't clear isPlaying here — let playback cleanup do it after I2S DMA is zeroed.
                if (isPlaying) {
                    interruptPlayback = true;
                }

                setLed(0, 255, 30, 255, 10); // green = transmitting
            } else if (!touched && pttHeld) {
                pttHeld = false;
                Serial.println("PTT: button UP");
                setLed(0, 100, 255, 100, 5); // dim blue = idle/listening
            }
        } else {
            // VOX: promote a pending normal tap to armed once its window has elapsed
            if (tapPendingArm && (millis() - lastTapTime >= DOUBLE_TAP_WINDOW_MS)) {
                tapPendingArm = false;
                doubleTapArmed = true;
            }
        }

        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

void gotTouch1()
{
    unsigned long currentTime = millis();

    // Debounce: ignore touches that occur too quickly after the last one
    if (currentTime - lastTouchTimeLeft < TOUCH_DEBOUNCE_MS)
    {
        return;
    }

    lastTouchTimeLeft = currentTime;
    Serial.println("Touch left [not implemented]");
}

void gotTouch3()
{
    unsigned long currentTime = millis();

    // Debounce: ignore touches that occur too quickly after the last one
    if (currentTime - lastTouchTimeRight < TOUCH_DEBOUNCE_MS)
    {
        return;
    }

    lastTouchTimeRight = currentTime;
    Serial.println("Touch right [not implemented]");
}

void gotTouch2() // center touch ISR
{
    // PTT mode uses polling in touchTask instead of ISR
    if (PTT_MODE) return;

    unsigned long currentTime = millis();
    if (currentTime - lastTouchTimeCenter < CENTER_DEBOUNCE_MS) return;
    lastTouchTimeCenter = currentTime;

    if (doubleTapArmed && (currentTime - lastTapTime < DOUBLE_TAP_WINDOW_MS)) {
        // Second tap of an armed pair → end call
        doubleTapArmed = false;
        tapPendingArm = false;
        handleDoubleTap();
        return;
    }

    bool wasNormalTap = handleShortPress();
    lastTapTime = currentTime;

    // Only "real" normal taps (extend / interrupt) qualify to arm a future double-tap.
    // No-ops (mute, no server) and re-enables reset arming so the next disable still
    // requires another standalone normal tap first.
    if (wasNormalTap) {
        tapPendingArm = true;
    } else {
        tapPendingArm = false;
        doubleTapArmed = false;
    }
}

// Returns true if this tap was a "real" normal tap on an already-enabled device
// (extending listen or interrupting playback). Returns false for no-ops and re-enables.
bool handleShortPress()
{
    Serial.println("Center touch: SHORT PRESS");

    if (mute || serverIP == IPAddress(0, 0, 0, 0))
    {
        setLed(255, 30, 0, 255, 10);
        return false;
    }

    if (!deviceEnabled)
    {
        deviceEnabled = true;
        mic_timeout = millis() + MIC_LISTEN_MS;
        setLed(255, 255, 255, 255, 8); // bright white flash = device enabled
        Serial.println("Device ENABLED");
        return false;
    }

    if (isPlaying)
    {
        Serial.println("Interrupting playback...");
        // Don't clear isPlaying here — playback cleanup zeroes I2S DMA before clearing it,
        // otherwise the mic loop can reopen while the speaker tail is still audible.
        interruptPlayback = true;
        mic_timeout = millis() + MIC_LISTEN_MS;
        setLed(255, 255, 255, 255, 8);
        return true;
    }

    mic_timeout = millis() + MIC_LISTEN_MS;
    setLed(255, 255, 255, 120, 8); // soft white pulse = mic re-activated
    return true;
}

void handleDoubleTap()
{
    Serial.println("Center touch: DOUBLE TAP - disabling device");

    mic_timeout = 0;

    if (isPlaying)
    {
        // Let playback cleanup clear isPlaying after I2S DMA is zeroed.
        interruptPlayback = true;
    }

    deviceEnabled = false;

    setLed(255, 40, 0, 200, 2); // slow red-orange pulse = device disabled

    Serial.println("Device DISABLED");
}

void enterConfigMode() {
    Serial.println("Entering configuration mode. Type 'exit' to save and restart, or 'cancel' to exit without saving.");
    Serial.println("Available commands: ssid, pass, server, timeout, volume");

    while (true) {
        if (Serial.available()) {
            String command = Serial.readStringUntil('\n');
            command.trim();

            if (command == "exit") {
                saveConfig();
                Serial.println("Configuration saved. Restarting...");
                delay(1000);
                ESP.restart();
            } else if (command == "cancel") {
                Serial.println("Exiting without saving. Restarting...");
                delay(1000);
                ESP.restart();
            } else if (command.startsWith("ssid ")) {
                wifi_ssid = command.substring(5);
                Serial.println("SSID set to: " + wifi_ssid);
            } else if (command.startsWith("pass ")) {
                wifi_password = command.substring(5);
                Serial.println("WiFi password updated");
            } else if (command.startsWith("server ")) {
                server_hostname = command.substring(7);
                Serial.println("Server hostname set to: " + server_hostname);
            } else if (command.startsWith("timeout ")) {
                mic_timeout_default = command.substring(8).toInt();
                Serial.println("Mic timeout set to: " + String(mic_timeout_default));
            } else if (command.startsWith("volume ")) {
                speaker_volume = command.substring(7).toInt();
                if (speaker_volume > 20) speaker_volume = 20;
                Serial.println("Speaker volume set to: " + String(speaker_volume));
            } else {
                Serial.println("Unknown command. Available commands: ssid, pass, server, timeout, volume");
            }
        }
    }
}

void loadConfig() {
    preferences.begin("onjuino-config", true);
    wifi_ssid = preferences.getString("wifi_ssid", WIFI_SSID);
    wifi_password = preferences.getString("wifi_pass", WIFI_PASSWORD);
    server_hostname = preferences.getString("server", DEFAULT_SERVER_HOSTNAME);
    mic_timeout_default = preferences.getInt("mic_timeout", DEFAULT_MIC_TIMEOUT);
    speaker_volume = preferences.getUChar("volume", DEFAULT_SPEAKER_VOLUME);
    uint32_t savedIP = preferences.getUInt("server_ip", 0);
    preferences.end();

    if (savedIP != 0)
        serverIP = IPAddress(savedIP);

    Serial.println("Loaded configuration:");
    Serial.println("SSID: " + wifi_ssid);
    Serial.println("Server: " + server_hostname);
    Serial.println("Mic Timeout: " + String(mic_timeout_default));
    Serial.println("Volume: " + String(speaker_volume));
    Serial.println("Saved IP: " + serverIP.toString());
}

void saveConfig() {
    preferences.begin("onjuino-config", false);
    preferences.putString("wifi_ssid", wifi_ssid);
    preferences.putString("wifi_pass", wifi_password);
    preferences.putString("server", server_hostname);
    preferences.putInt("mic_timeout", mic_timeout_default);
    preferences.putUChar("volume", speaker_volume);
    preferences.end();
}
