#include "ui/glitch_screen.h"

#include <Arduino.h>
#include <esp_random.h>

#include "hardware/display.h"

namespace {

// SMPTE 75% colour bars — top section
constexpr uint16_t kTopBars[7] = {
    0xC618,  // 75% white (grey)
    0xC5E0,  // 75% yellow
    0x05FF,  // 75% cyan
    0x05E0,  // 75% green
    0xC01F,  // 75% magenta
    0xC000,  // 75% red
    0x001B,  // 75% blue
};

// Bottom strip — blue / black / magenta / black / I-bar / black / white
constexpr uint16_t kBotBars[7] = {
    0x001F,  // blue
    0x0000,  // black
    0xF81F,  // magenta
    0x0000,  // black
    0x07FF,  // cyan (pluge)
    0x0000,  // black
    0xFFFF,  // white
};

constexpr int kTopH  = 170;
constexpr int kBotH  = 70;
constexpr int kBotY  = 240 - kBotH;
constexpr int kNBars = 7;

inline int barX(int b)    { return b * 240 / kNBars; }
inline int barW(int b)    { return barX(b + 1) - barX(b); }

void drawBase() {
    for (int b = 0; b < kNBars; b++) {
        tft.fillRect(barX(b), 0, barW(b), kTopH, kTopBars[b]);
    }
    tft.fillRect(0, kTopH, 240, kBotY - kTopH, 0x0000);
    for (int b = 0; b < kNBars; b++) {
        tft.fillRect(barX(b), kBotY, barW(b), kBotH, kBotBars[b]);
    }
}

void applyGlitch(uint32_t seed) {
    // Displaced horizontal strips
    const int strips = 4 + (seed & 7);
    for (int i = 0; i < strips; i++) {
        const int y      = esp_random() % 240;
        const int h      = 1 + esp_random() % 9;
        const int shift  = (int)(esp_random() % 60) - 30;
        const uint16_t c = kTopBars[esp_random() % kNBars];

        tft.fillRect(0, y, 240, h, 0x0000);
        for (int b = 0; b < kNBars; b++) {
            int x0 = barX(b) + shift;
            int w  = barW(b);
            if (x0 < 0)   { w += x0; x0 = 0; }
            if (x0 + w > 240) w = 240 - x0;
            if (w <= 0) continue;
            tft.fillRect(x0, y, w, h, c);
        }
    }

    // Random bright noise pixels
    for (int i = 0; i < 250; i++) {
        tft.drawPixel(esp_random() % 240, esp_random() % 240, 0xFFFF);
    }

    // Occasional full-width colour tear
    if ((seed & 0x3) == 0) {
        tft.drawFastHLine(0, esp_random() % 240, 240,
                          kTopBars[esp_random() % kNBars]);
    }

    // Occasional dark block
    if ((seed & 0x7) == 0) {
        tft.fillRect(0, esp_random() % 220, 240, 2 + esp_random() % 8, 0x0000);
    }
}

}  // namespace

void glitchScreenRun() {
    randomSeed(esp_random());
    drawBase();

    while (true) {
        applyGlitch(esp_random());
        delay(90);

        // Redraw clean base every ~10 frames to stop corruption building up
        static uint8_t frame = 0;
        if (++frame >= 10) {
            drawBase();
            frame = 0;
        }
    }
}
