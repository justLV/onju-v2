#ifndef AUDIO_COMPRESSION_H
#define AUDIO_COMPRESSION_H

#include <Arduino.h>

// μ-law (G.711) compression
// Reduces 16-bit PCM to 8-bit logarithmic encoding (2x compression)
// Stateless - each sample is independent

#define MULAW_BIAS 0x84
#define MULAW_MAX 0x1FFF

static const int16_t exp_lut[256] = {
    -32124,-31100,-30076,-29052,-28028,-27004,-25980,-24956,
    -23932,-22908,-21884,-20860,-19836,-18812,-17788,-16764,
    -15996,-15484,-14972,-14460,-13948,-13436,-12924,-12412,
    -11900,-11388,-10876,-10364,-9852,-9340,-8828,-8316,
    -7932,-7676,-7420,-7164,-6908,-6652,-6396,-6140,
    -5884,-5628,-5372,-5116,-4860,-4604,-4348,-4092,
    -3900,-3772,-3644,-3516,-3388,-3260,-3132,-3004,
    -2876,-2748,-2620,-2492,-2364,-2236,-2108,-1980,
    -1884,-1820,-1756,-1692,-1628,-1564,-1500,-1436,
    -1372,-1308,-1244,-1180,-1116,-1052,-988,-924,
    -876,-844,-812,-780,-748,-716,-684,-652,
    -620,-588,-556,-524,-492,-460,-428,-396,
    -372,-356,-340,-324,-308,-292,-276,-260,
    -244,-228,-212,-196,-180,-164,-148,-132,
    -120,-112,-104,-96,-88,-80,-72,-64,
    -56,-48,-40,-32,-24,-16,-8,0,
    32124,31100,30076,29052,28028,27004,25980,24956,
    23932,22908,21884,20860,19836,18812,17788,16764,
    15996,15484,14972,14460,13948,13436,12924,12412,
    11900,11388,10876,10364,9852,9340,8828,8316,
    7932,7676,7420,7164,6908,6652,6396,6140,
    5884,5628,5372,5116,4860,4604,4348,4092,
    3900,3772,3644,3516,3388,3260,3132,3004,
    2876,2748,2620,2492,2364,2236,2108,1980,
    1884,1820,1756,1692,1628,1564,1500,1436,
    1372,1308,1244,1180,1116,1052,988,924,
    876,844,812,780,748,716,684,652,
    620,588,556,524,492,460,428,396,
    372,356,340,324,308,292,276,260,
    244,228,212,196,180,164,148,132,
    120,112,104,96,88,80,72,64,
    56,48,40,32,24,16,8,0
};

inline uint8_t linear_to_ulaw(int16_t pcm_val) {
    int16_t sign = (pcm_val < 0) ? 0x80 : 0;
    if (sign) pcm_val = -pcm_val;
    if (pcm_val > MULAW_MAX) pcm_val = MULAW_MAX;

    pcm_val += MULAW_BIAS;
    int16_t exponent = 7;

    for (int16_t exp_mask = 0x4000; (pcm_val & exp_mask) == 0 && exponent > 0; exp_mask >>= 1, exponent--);

    int16_t mantissa = (pcm_val >> (exponent + 3)) & 0x0F;
    uint8_t ulaw_byte = ~(sign | (exponent << 4) | mantissa);

    return ulaw_byte;
}

inline int16_t ulaw_to_linear(uint8_t ulaw_byte) {
    return exp_lut[ulaw_byte];
}

// Encode buffer of PCM samples to μ-law
inline void encode_ulaw(const int16_t* pcm_in, uint8_t* ulaw_out, size_t sample_count) {
    for (size_t i = 0; i < sample_count; i++) {
        ulaw_out[i] = linear_to_ulaw(pcm_in[i]);
    }
}

// Decode buffer of μ-law samples to PCM
inline void decode_ulaw(const uint8_t* ulaw_in, int16_t* pcm_out, size_t sample_count) {
    for (size_t i = 0; i < sample_count; i++) {
        pcm_out[i] = ulaw_to_linear(ulaw_in[i]);
    }
}

#endif
