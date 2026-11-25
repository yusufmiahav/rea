#pragma once
#include <juce_audio_processors/juce_audio_processors.h>
#include <juce_gui_basics/juce_gui_basics.h>

#include "PluginProcessor.h"

namespace ui {
namespace col {
const juce::Colour bg{0xff12151a}, panel{0xff1a1f26}, panelHi{0xff232a33}, grid{0xff2b333d},
    text{0xffd9e1ea}, dim{0xff7d8a99}, accent{0xff2ec4b6}, cut{0xffff5d5d}, fixed{0xffffb547},
    meterLo{0xff2ec4b6}, meterMid{0xffffd166}, meterHi{0xffff5d5d};
}

class LookAndFeel : public juce::LookAndFeel_V4 {
public:
    LookAndFeel();
    void drawRotarySlider(juce::Graphics&, int x, int y, int w, int h, float pos, float a0, float a1, juce::Slider&) override;
    void drawToggleButton(juce::Graphics&, juce::ToggleButton&, bool hi, bool down) override;
    void drawButtonBackground(juce::Graphics&, juce::Button&, const juce::Colour&, bool hi, bool down) override;
};

// Vertical level meters (peak bar + RMS bar per channel) for the input or output.
class MeterBar : public juce::Component, public juce::SettableTooltipClient {
public:
    MeterBar(dfb::Meter* m, juce::String title) : meters(m), name(std::move(title)) {}
    void paint(juce::Graphics&) override;
    void tick(int channels);
private:
    dfb::Meter* meters;
    juce::String name;
    int nch = 2;
    float peakDb[dfb::kMaxChannels]{}, rmsDb[dfb::kMaxChannels]{}, holdDb[dfb::kMaxChannels]{};
    int holdAge[dfb::kMaxChannels]{};
};

// Live spectrum (log frequency), the combined cut curve, and a marker per notch.
class SpectrumView : public juce::Component, public juce::SettableTooltipClient {
public:
    explicit SpectrumView(dfb::Engine& e) : engine(e) {}
    void paint(juce::Graphics&) override;
    void tick() { repaint(); }
private:
    float xFor(double f) const;
    dfb::Engine& engine;
};

// The 12 notch slots as a strip of cells.
class NotchList : public juce::Component {
public:
    explicit NotchList(dfb::Engine& e) : engine(e) {}
    void paint(juce::Graphics&) override;
private:
    dfb::Engine& engine;
};
} // namespace ui

class DeFeedbackEditor : public juce::AudioProcessorEditor, private juce::Timer {
public:
    explicit DeFeedbackEditor(DeFeedbackProcessor&);
    ~DeFeedbackEditor() override;
    void paint(juce::Graphics&) override;
    void resized() override;
    void refreshNow() { timerCallback(); }

private:
    void timerCallback() override;
    using SliderAtt = juce::AudioProcessorValueTreeState::SliderAttachment;
    using ButtonAtt = juce::AudioProcessorValueTreeState::ButtonAttachment;
    using ComboAtt = juce::AudioProcessorValueTreeState::ComboBoxAttachment;

    struct Knob {
        juce::Slider slider{juce::Slider::RotaryHorizontalVerticalDrag, juce::Slider::TextBoxBelow};
        juce::Label label;
        std::unique_ptr<SliderAtt> att;
    };
    void setupKnob(Knob&, const juce::String& id, const juce::String& title, const juce::String& tip);

    DeFeedbackProcessor& proc;
    ui::LookAndFeel lnf;
    ui::MeterBar inMeter, outMeter;
    ui::SpectrumView spectrum;
    ui::NotchList notches;
    Knob sens, depth, width, hold;
    juce::ComboBox mode;
    juce::ToggleButton lock{"Lock all"}, freeze{"Freeze"}, bypass{"Bypass"};
    juce::TextButton clearAll{"Clear all"}, clearLive{"Clear live"};
    juce::Label sourceLabel, status, modelInfo;
    std::unique_ptr<ComboAtt> modeAtt;
    std::unique_ptr<ButtonAtt> lockAtt, freezeAtt, bypassAtt;
    juce::TooltipWindow tooltips{this, 500};
    int lastEvents = 0;
    double flashUntil = 0;

    JUCE_DECLARE_NON_COPYABLE_WITH_LEAK_DETECTOR(DeFeedbackEditor)
};
