// Renders the plugin UI to a PNG after feeding it simulated feedback, so CI
// (and reviewers) can see the editor without a DAW.  usage: dfb_snapshot out.png
#include <juce_audio_processors/juce_audio_processors.h>
#include <juce_gui_basics/juce_gui_basics.h>

#include "../source/PluginEditor.h"
#include "../source/PluginProcessor.h"
#include "sim.h"

int main(int argc, char** argv) {
    juce::ScopedJuceInitialiser_GUI init;
    const juce::String out = argc > 1 ? argv[1] : "snapshot.png";
    const double fs = 48000;
    DeFeedbackProcessor proc;
    proc.setPlayConfigDetails(2, 2, fs, 256);
    proc.engine.setThreaded(false); // deterministic offline render
    proc.prepareToPlay(fs, 256);
    sim::Rng r(7);
    auto src = sim::makeSource(sim::Src::Sung, fs, 10, r);
    auto h = sim::makeIR(fs, r, (int)(fs * 0.12));
    auto gain = [](double t) { return -6.0 + 1.4 * t; };
    juce::MidiBuffer midi;
    juce::AudioBuffer<float> buf(2, 64);
    sim::runLoop(src, h, fs, gain, [&](float* const* ch, int m) {
        buf.setSize(2, m, false, false, true);
        buf.copyFrom(0, 0, ch[0], m); buf.copyFrom(1, 0, ch[0], m);
        proc.processBlock(buf, midi);
        juce::FloatVectorOperations::copy(ch[0], buf.getReadPointer(0), m);
    });
    std::unique_ptr<juce::AudioProcessorEditor> ed(proc.createEditor());
    ed->setSize(960, 600);
    for (int i = 0; i < 40; ++i) { // let meters/peak holds settle like the 30 Hz timer would
        if (auto* e = dynamic_cast<DeFeedbackEditor*>(ed.get())) e->refreshNow();
    }
    auto img = ed->createComponentSnapshot(ed->getLocalBounds(), true, 2.0f);
    juce::File f = juce::File::getCurrentWorkingDirectory().getChildFile(out);
    f.deleteFile();
    juce::FileOutputStream os(f);
    juce::PNGImageFormat png;
    const bool ok = os.openedOk() && png.writeImageToStream(img, os);
    std::printf("%s %s\n", ok ? "wrote" : "failed", f.getFullPathName().toRawUTF8());
    return ok ? 0 : 1;
}
