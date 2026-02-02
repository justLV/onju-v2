#include <WiFi.h>
#include <WiFiUdp.h>
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
#define HOST_NAME "onju-coral"

#include "custom_boards.h"
#include "credentials.h"
#include "audio_compression.h"

#define TOUCH_EN
#define DISABLE_HARDWARE_MUTE  // Temporary: disable mute switch check

// Wi-Fi settings - edit these in credentials.h
Preferences preferences;

// Define default values
#define DEFAULT_SERVER_HOSTNAME "default.server.com"
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

const double gammaValue = 1.8; // dropped this down from typical 2.2 to avoid flicker
uint8_t gammaCorrectionTable[256];

// Speaker buffer settings
const size_t tcpBufferSize = 512; // for received audio data before processing into 32-bit chunks for MAX98357A
uint8_t tcpBuffer[tcpBufferSize];

int32_t *wavData = NULL; // assign later as PSRAM (or not) as a buffer for playback from TCP

// how many samples to load from TCP before starting playing (avoid jitter due to running out of data w/ bad wifi)
#ifdef USE_PSRAM
int bufferThreshold = 8192;
#else
int bufferThreshold = 512;
#endif

// Mic settings
#define SAMPLE_CHUNK_SIZE 480                  // chosen to be 30ms (at 16kHz) for WebRTC VAD, and fit within UDP packet as int16 (480 * 2 < 1400)
int32_t micBuffer[SAMPLE_CHUNK_SIZE];          // For raw values from I2S
int16_t convertedMicBuffer[SAMPLE_CHUNK_SIZE]; // For converted values to be sent over UDP

#define MAX_ALLOWED_OFFSET 16000
#define MIC_OFFSET_AVERAGING_FRAMES 1
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

    char desired_hostname[50];
    snprintf(desired_hostname, sizeof(desired_hostname), "%s-%s", HOST_NAME, BOARD_NAME);

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

    Serial.println("Sending multicast packet to announce presence");
    udp.beginPacket(IPAddress(239, 0, 0, 1), 12345);
    String mcast_string = String(hostname) + " " + String(GIT_HASH);
    udp.write(reinterpret_cast<const uint8_t *>(mcast_string.c_str()), mcast_string.length());
    udp.endPacket();

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

        serverIP = client.remoteIP();

        while (client.available() < 6) // TODO: timeout
        {
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
            leds.clear();
            leds.show();
            uint16_t timeout = header[1] << 8 | header[2];
            speaker_volume = header[3];
            uint8_t compression_type = header[5];
            setLed(255, 255, 255, 0, header[4]); // header[4] sets fade rate. hardcoding to white but different voices could have different colors in future

            Serial.printf("Received audio (compression=%d) with mic timeout %d seconds, volume %d\n",
                         compression_type, timeout, speaker_volume);

            if (speaker_volume > 20)
            {
                speaker_volume = 20;
            }

            isPlaying = true;

            bool initialBufferFilled = false; // get a nice reservoir loaded into wavData to try avoid jitter
            uint32_t tic = millis();
            size_t totalSamplesRead = 0;

            size_t bytesAvailable, bytesToRead, bytesRead, bytesWritten, bytesToWrite;
            int16_t sample16;
            uint32_t sum = 0; // for calculating average for LEDs
            bool wasInterrupted = false; // Track if playback was interrupted

            // Handle Opus compressed audio
            if (compression_type == 2 && opus_decoder != NULL)
            {
                Serial.println("Starting Opus decode task with 32KB stack");
                opusClient = &client;
                opusTaskRunning = true;
                interruptPlayback = false; // Clear interrupt flag

                // Create dedicated task with large stack for Opus decoding
                xTaskCreatePinnedToCore(
                    opusDecodeTask,
                    "OpusDecodeTask",
                    32768,  // 32KB stack for Opus decoder
                    NULL,
                    1,
                    NULL,
                    1
                );

                // Wait for task to finish or interrupt
                while (opusTaskRunning)
                {
                    delay(100);
                }

                // Remember if we were interrupted (before clearing flag)
                wasInterrupted = interruptPlayback;

                // If interrupted, drain remaining TCP data without playing
                if (interruptPlayback)
                {
                    Serial.println("Draining TCP buffer after interrupt...");

                    // Clear I2S DMA buffer to stop audio immediately
                    i2s_zero_dma_buffer(I2S_NUM);

                    // Drain TCP for up to 1 second
                    uint32_t drainStart = millis();
                    while (client.connected() && (millis() - drainStart) < 1000)
                    {
                        if (client.available() >= 2)
                        {
                            // Read frame length
                            uint8_t len_bytes[2];
                            client.read(len_bytes, 2);
                            uint16_t frame_len = (len_bytes[0] << 8) | len_bytes[1];

                            if (frame_len > 0 && frame_len <= OPUS_MAX_PACKET)
                            {
                                // Read and discard frame data
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
                    interruptPlayback = false; // Clear flag
                }
                else
                {
                    Serial.println("Opus decode task completed normally");
                }
            }
            // Handle PCM audio (compression_type == 0)
            else
            {
                while (client.connected())
                {
                    // Check for user interrupt
                    if (interruptPlayback)
                    {
                        Serial.println("PCM playback interrupted by user");
                        break;
                    }

                    bytesAvailable = client.available();

                    if (bytesAvailable >= 2)
                    {
                        bytesToRead = (bytesAvailable / 2) * 2; // ensure whole samples only
                        if (bytesToRead > tcpBufferSize)
                        {
                            bytesToRead = tcpBufferSize;
                        }

                        bytesRead = client.read(tcpBuffer, bytesToRead);

                        for (size_t i = 0; i < bytesRead; i += 2)
                        {
                            sample16 = (tcpBuffer[i + 1] << 8) | tcpBuffer[i];
                            wavData[totalSamplesRead++] = (int32_t)sample16 << speaker_volume; // crude volume control
                        }
                    // Start draining once we have a "good" reservoir
                    if (initialBufferFilled || totalSamplesRead >= bufferThreshold)
                    {
                        if (!initialBufferFilled)
                        {
                            Serial.println("Initial buffer filled. totalSamplesRead: " + String(totalSamplesRead));
                            initialBufferFilled = true;
                        }

                        bytesToWrite = totalSamplesRead * 4; // int32_t
                        bytesWritten = 0;

                        i2s_write(I2S_NUM, (uint8_t *)wavData, bytesToWrite, &bytesWritten, portMAX_DELAY);

                        if (millis() - tic > 30)
                        {
                            tic = millis();
                            for (int i = 0; i < 128; i += 4)
                            {
                                sum += abs(wavData[i]); // abs() is faster than squaring
                            }
                            uint8_t sum_u8 = sum >> (speaker_volume + 8); // LEDs independent of volume

                            if (sum_u8 > ledLevel)
                            { // should only ramp down naturally
                                ledLevel = sum_u8;
                                Serial.println("ledLevel: " + String(ledLevel));
                            }
                            sum = 0;
                        }
                        totalSamplesRead = 0;
                    }
                }
                    else
                    {
                        delay(2); // Allow for some bytes to be ready before reading again
                    }
                }

                // Remember if we were interrupted (before clearing flag)
                wasInterrupted = interruptPlayback;

                // If interrupted, drain remaining TCP data without playing
                if (interruptPlayback)
                {
                    Serial.println("Draining PCM TCP buffer after interrupt...");

                    // Clear I2S DMA buffer to stop audio immediately
                    i2s_zero_dma_buffer(I2S_NUM);

                    // Drain TCP for up to 1 second
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
                    interruptPlayback = false; // Clear flag
                }
            } // end else (PCM handling)

            // Only flush silence if not interrupted
            if (!wasInterrupted)
            {
                // Hack to fill buffers with silence and block till all real audio is flushed out
                uint32_t silenceBuffer[240];
                memset(silenceBuffer, 0, sizeof(silenceBuffer));
                for (int i = 0; i < 8; i++)
                {
                    size_t bytesWritten = 0;
                    i2s_write(I2S_NUM, silenceBuffer, sizeof(silenceBuffer), &bytesWritten, portMAX_DELAY);
                }
            }

            isPlaying = false;

            mic_timeout = millis() + timeout * 1000;
            Serial.println("Done loading audio in buffers in " + String(millis() - tic) + "ms");
            Serial.println("Set mic_timeout to " + String(mic_timeout));
        }
        /*
        header[0]   0xBB for set LED command
        header[1]   bitmask of which LED's to set
        header[2:4] RGB color
        */
        else if (header[0] == 0xBB)
        {
            Serial.println("Received custom LED command (0xBB)");
            setLed(0, 0, 0, 0, 0); // stop ramping down
            uint8_t bitmask = header[1];
            for (int i = 0; i < 6; i++)
            {
                if (bitmask & (1 << i))
                {
                    leds.setPixelColor(i, header[2], header[3], header[4]);
                }
            }
            leds.show();
            client.stop();
        }
        /*
        header[0]   0xCC for LED blink command
        header[1]   starting intensity for rampdown
        header[2:4] RGB color
        header[5]   fade rate
        */
        else if (header[0] == 0xCC)
        {
            Serial.println("Received LED blink command (0xCC)");
            setLed(header[2], header[3], header[4], header[1], header[5]);
            client.stop();

            if(mic_timeout > millis()) // if already listening...
            {
                if (mic_timeout < (millis() + VAD_MIC_EXTEND)) // and about to run out of time...
                {
                    mic_timeout = millis() + VAD_MIC_EXTEND; // ... extend to not cut-off
                    Serial.println("Extended mic timeout to " + String(mic_timeout));
                }
            }
        }
        /*
        header[0]   0xDD for mic timeout command - added to stop listening while server is thinking
        header[1:2] mic timeout in seconds typically set to 0 in this use case
        header[3:5] not used
        */
        else if (header[0] == 0xDD)
        {
            Serial.println("Received mic timeout command (0xDD)");
            uint16_t timeout = header[1] << 8 | header[2];
            mic_timeout = millis() + timeout;
            setLed(0, 255, 50, 100, 5); // TODO add better thinking animation - currently just green pulse to indicate transcribe is done
            client.stop();
        }
        else
        {
            Serial.println("Received unknown command");
            setLed(255, 0, 0, 255, 6);
            client.stop();
        }
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

    while (client->connected())
    {
        // Check for user interrupt
        if (interruptPlayback)
        {
            Serial.println("Playback interrupted by user");
            break;
        }
        // Read 2-byte frame length
        if (client->available() < 2)
        {
            delay(1);
            continue;
        }

        uint8_t len_bytes[2];
        client->read(len_bytes, 2);
        uint16_t frame_len = (len_bytes[0] << 8) | len_bytes[1];

        // Sanity check
        if (frame_len == 0 || frame_len > OPUS_MAX_PACKET)
        {
            Serial.printf("Invalid Opus frame length: %d\n", frame_len);
            break;
        }

        // Read Opus frame
        size_t bytes_read = 0;
        while (bytes_read < frame_len && client->connected())
        {
            int avail = client->available();
            if (avail > 0)
            {
                int to_read = min(avail, (int)(frame_len - bytes_read));
                bytes_read += client->read(opus_packet_buffer + bytes_read, to_read);
            }
            else
            {
                delay(1);
            }
        }

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

    int counter = 0;
    bool prevState = false;

    while (1)
    {
        bool currentState = false;
        if (isPlaying || mute) // don't listen while playing audio or muted
            ;
        else if (serverIP == IPAddress(0, 0, 0, 0)) // no server greeted us yet, so nowhere to send data
            ;
        else if (mic_timeout < millis()) // alotted time for speaking has passed
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

            // Convert to 16-bit and calculate DC offset
            int32_t dc_sum = 0;
            for (int i = 0; i < sizeof(micBuffer) / sizeof(micBuffer[0]); i++)
            {
                convertedMicBuffer[i] = static_cast<int16_t>(micBuffer[i] >> 14);
                dc_sum += convertedMicBuffer[i];
            }
            int16_t dc_offset = dc_sum / SAMPLE_CHUNK_SIZE;

            // Remove DC offset from all samples
            for (int i = 0; i < SAMPLE_CHUNK_SIZE; i++)
            {
                convertedMicBuffer[i] -= dc_offset;
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

void gotTouch2() // center touch
{
    unsigned long currentTime = millis();

    // Debounce: ignore touches that occur too quickly after the last one
    if (currentTime - lastTouchTimeCenter < TOUCH_DEBOUNCE_MS)
    {
        // Touch ignored due to debounce
        return;
    }

    lastTouchTimeCenter = currentTime;
    Serial.println("Center touch");

    if (mute || serverIP == IPAddress(0, 0, 0, 0))
    {
        setLed(255, 30, 0, 255, 10); // cannot listen
    }
    else if (isPlaying)
    {
        // Interrupt assistant playback
        Serial.println("Interrupting playback...");
        interruptPlayback = true;
        isPlaying = false; // Mark as not playing immediately

        // Enable microphone immediately (30s to speak)
        mic_timeout = millis() + 30000;

        // Visual feedback: green = listening
        setLed(0, 255, 30, 255, 10);
    }
    else if (mic_timeout < (millis() + 30000))
    {
        // give 30 seconds to speak
        mic_timeout = millis() + 30000;
        setLed(0, 255, 30, 255, 10);
    }
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
    preferences.begin("onjuino-config", false);
    wifi_ssid = preferences.getString("wifi_ssid", WIFI_SSID);
    wifi_password = preferences.getString("wifi_pass", WIFI_PASSWORD);
    server_hostname = preferences.getString("server", DEFAULT_SERVER_HOSTNAME);
    mic_timeout_default = preferences.getInt("mic_timeout", DEFAULT_MIC_TIMEOUT);
    speaker_volume = preferences.getUChar("volume", DEFAULT_SPEAKER_VOLUME);
    preferences.end();

    Serial.println("Loaded configuration:");
    Serial.println("SSID: " + wifi_ssid);
    Serial.println("Server: " + server_hostname);
    Serial.println("Mic Timeout: " + String(mic_timeout_default));
    Serial.println("Volume: " + String(speaker_volume));
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
