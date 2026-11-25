#include "PluginProcessor.h"
#include "PluginEditor.h"

using APVTS = juce::AudioProcessorValueTreeState;

static juce::String widthText(float oct, int) {
    const int denom = juce::roundToInt(1.0f / juce::jmax(oct, 0.01f));
    return "1/" + juce::String(denom) + " oct";
}

APVTS::ParameterLayout DeFeedbackProcessor::createLayout() {
    APVTS::ParameterLayout l;
    l.add(std::make_unique<juce::AudioParameterFloat>(juce::ParameterID{"sens", 1}, "Sensitivity",
        juce::NormalisableRange<float>(0.f, 100.f, 1.f), 50.f, juce::AudioParameterFloatAttributes().withLabel("%")));
    l.add(std::make_unique<juce::AudioParameterChoice>(juce::ParameterID{"mode", 1}, "Mode",
        juce::StringArray{"Live", "Ring-out"}, 0));
    l.add(std::make_unique<juce::AudioParameterFloat>(juce::ParameterID{"depth", 1}, "Max cut",
        juce::NormalisableRange<float>(3.f, 24.f, 0.5f), 15.f, juce::AudioParameterFloatAttributes().withLabel("dB")));
    l.add(std::make_unique<juce::AudioParameterFloat>(juce::ParameterID{"width", 1}, "Width",
        juce::NormalisableRange<float>(1.f / 20.f, 1.f / 3.f, 0.f, 0.5f), 1.f / 8.f,
        juce::AudioParameterFloatAttributes().withStringFromValueFunction(widthText)));
    l.add(std::make_unique<juce::AudioParameterFloat>(juce::ParameterID{"hold", 1}, "Hold",
        juce::NormalisableRange<float>(1.f, 60.f, 0.5f, 0.5f), 8.f, juce::AudioParameterFloatAttributes().withLabel("s")));
    l.add(std::make_unique<juce::AudioParameterBool>(juce::ParameterID{"lock", 1}, "Lock all", false));
    l.add(std::make_unique<juce::AudioParameterBool>(juce::ParameterID{"freeze", 1}, "Freeze", false));
    l.add(std::make_unique<juce::AudioParameterBool>(juce::ParameterID{"bypass", 1}, "Bypass", false));
    return l;
}

DeFeedbackProcessor::DeFeedbackProcessor()
    : AudioProcessor(BusesProperties()
                         .withInput("Input", juce::AudioChannelSet::stereo(), true)
                         .withOutput("Output", juce::AudioChannelSet::stereo(), true)),
      apvts(*this, nullptr, "params", createLayout()) {
    pSens = apvts.getRawParameterValue("sens");
    pMode = apvts.getRawParameterValue("mode");
    pDepth = apvts.getRawParameterValue("depth");
    pWidth = apvts.getRawParameterValue("width");
    pHold = apvts.getRawParameterValue("hold");
    pLock = apvts.getRawParameterValue("lock");
    pFreeze = apvts.getRawParameterValue("freeze");
    pBypass = apvts.getRawParameterValue("bypass");
}

bool DeFeedbackProcessor::isBusesLayoutSupported(const BusesLayout& layouts) const {
    const auto out = layouts.getMainOutputChannelSet();
    if (out != juce::AudioChannelSet::mono() && out != juce::AudioChannelSet::stereo()) return false;
    return layouts.getMainInputChannelSet() == out;
}

void DeFeedbackProcessor::prepareToPlay(double sampleRate, int samplesPerBlock) {
    // keep fixed cuts across a re-prepare (sample-rate change, transport restart)
    std::vector<Fixed> keep;
    for (const auto& s : engine.bank.slots)
        if (s.active && s.locked) keep.push_back({s.freq, s.targetDb, s.targetOct});
    engine.prepare(sampleRate, juce::jmax(1, getTotalNumOutputChannels()), samplesPerBlock);
    const juce::SpinLock::ScopedLockType sl(pendingLock);
    if (!hasPending && !keep.empty()) { pendingFixed = keep; hasPending = true; }
    lastLockAll = lastRingOut = false;
}

void DeFeedbackProcessor::processBlock(juce::AudioBuffer<float>& buffer, juce::MidiBuffer&) {
    juce::ScopedNoDenormals noDenormals;
    for (int c = getTotalNumInputChannels(); c < getTotalNumOutputChannels(); ++c) buffer.clear(c, 0, buffer.getNumSamples());

    if (clearRequest.exchange(false)) engine.bank.clear(false);
    if (clearLiveRequest.exchange(false)) engine.bank.clear(true);
    {
        const juce::SpinLock::ScopedTryLockType tl(pendingLock);
        if (tl.isLocked() && hasPending) {
            if (resetBeforeRestore) { engine.bank.reset(); resetBeforeRestore = false; }
            for (const auto& f : pendingFixed) engine.bank.addFixed(f.f, f.db, f.oct);
            pendingFixed.clear(); hasPending = false;
        }
    }

    engine.setSensitivity(pSens->load() / 100.f);
    const bool ringOut = pMode->load() > 0.5f, lock = pLock->load() > 0.5f;
    if (ringOut != lastRingOut) { engine.setRingOut(ringOut); lastRingOut = ringOut; }
    if (lock != lastLockAll) { engine.setLockAll(lock); lastLockAll = lock; }
    auto& st = engine.bank.settings;
    st.maxDepthDb = pDepth->load();
    st.widthOct = pWidth->load();
    st.holdS = pHold->load();
    engine.frozen = pFreeze->load() > 0.5f;
    engine.bypass = pBypass->load() > 0.5f;

    engine.process(buffer.getArrayOfWritePointers(), juce::jmin(buffer.getNumChannels(), dfb::kMaxChannels), buffer.getNumSamples());
}

bool DeFeedbackProcessor::setLearning(bool on) {
    if (on == isLearning()) return true;
    if (!on) {
        engine.learn = false;
        drainLearn();
        learnLog.close();
        stopTimer();
        return true;
    }
    auto dir = juce::File::getSpecialLocation(juce::File::userDocumentsDirectory).getChildFile("De-Feedback AI");
    dir.createDirectory();
    auto safe = juce::File::createLegalFileName(sourceName).replaceCharacter(' ', '-');
    learnFile = dir.getChildFile("learn-" + juce::Time::getCurrentTime().formatted("%Y%m%d-%H%M%S") + "-" + safe + ".csv");
    if (!learnLog.open(learnFile.getFullPathName().toStdString())) return false;
    learnScratch.resize(dfb::Engine::kLearnCap);
    learnMarks = 0;
    engine.learn = true;
    startTimerHz(4);
    return true;
}

void DeFeedbackProcessor::drainLearn() {
    if (!learnLog.isOpen()) return;
    int n;
    while ((n = engine.popLearn(learnScratch.data(), (int)learnScratch.size())) > 0)
        for (int i = 0; i < n; ++i) {
            if (learnScratch[i].kind == 1) lastCutFreq = learnScratch[i].freq;
            learnLog.write(learnScratch[i]);
        }
    learnLog.flush();
}

void DeFeedbackProcessor::timerCallback() { drainLearn(); }

void DeFeedbackProcessor::markLastCut(bool wasFeedback) {
    if (!isLearning()) return;
    drainLearn();
    const float f = engine.lastEventFreq.load();
    learnLog.mark(engine.nowSeconds(), wasFeedback ? 1 : 0, f > 0 ? f : lastCutFreq);
    ++learnMarks;
}

juce::AudioProcessorEditor* DeFeedbackProcessor::createEditor() { return new DeFeedbackEditor(*this); }

void DeFeedbackProcessor::getStateInformation(juce::MemoryBlock& destData) {
    auto state = apvts.copyState();
    juce::ValueTree fixed("fixed");
    for (int i = 0; i < dfb::kMaxNotches; ++i) {
        const auto& v = engine.slotView[i];
        if (v.state.load() == 2) {
            juce::ValueTree n("notch");
            n.setProperty("f", v.freq.load(), nullptr);
            n.setProperty("db", v.db.load(), nullptr);
            n.setProperty("oct", v.oct.load(), nullptr);
            fixed.addChild(n, -1, nullptr);
        }
    }
    state.removeChild(state.getChildWithName("fixed"), nullptr);
    state.addChild(fixed, -1, nullptr);
    state.setProperty("source", sourceName, nullptr);
    if (auto xml = state.createXml()) copyXmlToBinary(*xml, destData);
}

void DeFeedbackProcessor::setStateInformation(const void* data, int sizeInBytes) {
    auto xml = getXmlFromBinary(data, sizeInBytes);
    if (!xml) return;
    auto state = juce::ValueTree::fromXml(*xml);
    if (!state.hasType(apvts.state.getType())) return;
    sourceName = state.getProperty("source", sourceName).toString();
    std::vector<Fixed> fx;
    for (auto n : state.getChildWithName("fixed"))
        fx.push_back({(double)n.getProperty("f"), (double)n.getProperty("db"), (double)n.getProperty("oct")});
    state.removeChild(state.getChildWithName("fixed"), nullptr);
    apvts.replaceState(state);
    const juce::SpinLock::ScopedLockType sl(pendingLock);
    pendingFixed = fx; hasPending = true;
    resetBeforeRestore = true; // drop current cuts, then restore the saved fixed ones
}

juce::AudioProcessor* JUCE_CALLTYPE createPluginFilter() { return new DeFeedbackProcessor(); }
