#pragma once
#include <juce_audio_processors/juce_audio_processors.h>

#include "engine.h"

class DeFeedbackProcessor : public juce::AudioProcessor {
public:
    DeFeedbackProcessor();
    ~DeFeedbackProcessor() override = default;

    void prepareToPlay(double sampleRate, int samplesPerBlock) override;
    void releaseResources() override {}
    bool isBusesLayoutSupported(const BusesLayout& layouts) const override;
    void processBlock(juce::AudioBuffer<float>&, juce::MidiBuffer&) override;

    juce::AudioProcessorEditor* createEditor() override;
    bool hasEditor() const override { return true; }

    const juce::String getName() const override { return "De-Feedback AI"; }
    bool acceptsMidi() const override { return false; }
    bool producesMidi() const override { return false; }
    bool isMidiEffect() const override { return false; }
    double getTailLengthSeconds() const override { return 0.0; }

    int getNumPrograms() override { return 1; }
    int getCurrentProgram() override { return 0; }
    void setCurrentProgram(int) override {}
    const juce::String getProgramName(int) override { return {}; }
    void changeProgramName(int, const juce::String&) override {}

    void getStateInformation(juce::MemoryBlock& destData) override;
    void setStateInformation(const void* data, int sizeInBytes) override;

    static juce::AudioProcessorValueTreeState::ParameterLayout createLayout();

    juce::AudioProcessorValueTreeState apvts;
    dfb::Engine engine;
    std::atomic<bool> clearRequest{false}, clearLiveRequest{false};
    juce::String sourceName{"Qu-16 Ch 1"};

private:
    struct Fixed { double f, db, oct; };
    juce::SpinLock pendingLock;
    std::vector<Fixed> pendingFixed;
    bool hasPending = false, resetBeforeRestore = false, lastLockAll = false, lastRingOut = false;

    std::atomic<float>* pSens = nullptr; std::atomic<float>* pMode = nullptr;
    std::atomic<float>* pDepth = nullptr; std::atomic<float>* pWidth = nullptr;
    std::atomic<float>* pHold = nullptr; std::atomic<float>* pLock = nullptr;
    std::atomic<float>* pFreeze = nullptr; std::atomic<float>* pBypass = nullptr;

    JUCE_DECLARE_NON_COPYABLE_WITH_LEAK_DETECTOR(DeFeedbackProcessor)
};
