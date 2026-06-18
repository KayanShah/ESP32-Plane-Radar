#pragma once

#include <cstddef>
#include <cstdint>

namespace ui::radar {

/**
 * Range presets (label on ring 3 = ¾ of outer radius).
 *
 * Recommended for ADS-B on a 1.28″ display:
 *   5 km  — pattern / very local (airfield vicinity)
 *  10 km  — default; neighborhood spotting
 *  15 km  — wider local area
 *  25 km  — metro / regional picture
 *
 * Outer radius (for aircraft math) is ring-3 distance ÷ 0.75.
 */
struct RangePreset {
  /** Distance shown on ring 3 (¾ of outer radius), always stored in km. */
  float ring3_km;
  float outer_km;
};

constexpr float kRing3ToOuterKm = 4.0f / 3.0f;

constexpr RangePreset kRangePresets[] = {
    {0.5f,  0.5f  * kRing3ToOuterKm},
    {1.0f,  1.0f  * kRing3ToOuterKm},
    {2.0f,  2.0f  * kRing3ToOuterKm},
    {3.0f,  3.0f  * kRing3ToOuterKm},
    {4.0f,  4.0f  * kRing3ToOuterKm},
    {5.0f,  5.0f  * kRing3ToOuterKm},
    {6.0f,  6.0f  * kRing3ToOuterKm},
    {7.0f,  7.0f  * kRing3ToOuterKm},
    {8.0f,  8.0f  * kRing3ToOuterKm},
    {9.0f,  9.0f  * kRing3ToOuterKm},
    {10.0f, 10.0f * kRing3ToOuterKm},
    {11.0f, 11.0f * kRing3ToOuterKm},
    {12.0f, 12.0f * kRing3ToOuterKm},
    {13.0f, 13.0f * kRing3ToOuterKm},
    {14.0f, 14.0f * kRing3ToOuterKm},
    {15.0f, 15.0f * kRing3ToOuterKm},
    {16.0f, 16.0f * kRing3ToOuterKm},
    {17.0f, 17.0f * kRing3ToOuterKm},
    {18.0f, 18.0f * kRing3ToOuterKm},
    {19.0f, 19.0f * kRing3ToOuterKm},
    {20.0f, 20.0f * kRing3ToOuterKm},
    {21.0f, 21.0f * kRing3ToOuterKm},
    {22.0f, 22.0f * kRing3ToOuterKm},
    {23.0f, 23.0f * kRing3ToOuterKm},
    {24.0f, 24.0f * kRing3ToOuterKm},
    {25.0f, 25.0f * kRing3ToOuterKm},
};

constexpr size_t kRangePresetCount =
    sizeof(kRangePresets) / sizeof(kRangePresets[0]);

/** Load saved range and distance units from flash. Call once after boot. */
void rangeInit();
/** Cycle preset and save to flash. */
void rangeNext();
const RangePreset& rangeCurrent();
uint8_t rangeIndex();
/** ADSB fetch radius (km): scaled to screen edge so beyond-ring dots have data. */
float fetchRadiusKm();

bool useMiles();
bool showRunways();
/** WiFi portal checkbox: "T" = miles, otherwise km. */
void saveMilesFromPortal(const char* checkbox_value);
void saveRunwaysFromPortal(const char* checkbox_value);
void formatRing3Label(char* buf, size_t len, float ring3_km, bool use_miles);
void formatCurrentRing3Label(char* buf, size_t len);
/** Reset distance units to km (e.g. with WiFi credential wipe). */
void unitsReset();

}  // namespace ui::radar
