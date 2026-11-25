#include "PluginEditor.h"

using namespace ui;

static float toDb(float g) { return g > 1e-6f ? 20.f * std::log10(g) : -120.f; }

// ---------------------------------------------------------------- look and feel
LookAndFeel::LookAndFeel() {
    setColour(juce::ResizableWindow::backgroundColourId, col::bg);
    setColour(juce::Slider::textBoxTextColourId, col::text);
    setColour(juce::Slider::textBoxOutlineColourId, juce::Colours::transparentBlack);
    setColour(juce::Slider::textBoxBackgroundColourId, col::panelHi);
    setColour(juce::Label::textColourId, col::text);
    setColour(juce::ComboBox::backgroundColourId, col::panelHi);
    setColour(juce::ComboBox::outlineColourId, col::grid);
    setColour(juce::ComboBox::textColourId, col::text);
    setColour(juce::ComboBox::arrowColourId, col::accent);
    setColour(juce::PopupMenu::backgroundColourId, col::panel);
    setColour(juce::PopupMenu::highlightedBackgroundColourId, col::accent.withAlpha(0.3f));
    setColour(juce::TextButton::buttonColourId, col::panelHi);
    setColour(juce::TextButton::textColourOffId, col::text);
    setColour(juce::TextEditor::backgroundColourId, col::panelHi);
    setColour(juce::TextEditor::textColourId, col::text);
    setColour(juce::TextEditor::outlineColourId, col::grid);
    setColour(juce::TextEditor::focusedOutlineColourId, col::accent);
    setColour(juce::TooltipWindow::backgroundColourId, col::panelHi);
    setColour(juce::TooltipWindow::textColourId, col::text);
    setColour(juce::TooltipWindow::outlineColourId, col::grid);
}

void LookAndFeel::drawRotarySlider(juce::Graphics& g, int x, int y, int w, int h, float pos, float a0, float a1, juce::Slider&) {
    auto r = juce::Rectangle<float>((float)x, (float)y, (float)w, (float)h).reduced(6.f);
    const float size = juce::jmin(r.getWidth(), r.getHeight());
    r = r.withSizeKeepingCentre(size, size);
    const auto c = r.getCentre();
    const float rad = size * 0.5f, thick = juce::jmax(3.f, size * 0.09f);
    const float ang = a0 + pos * (a1 - a0);
    juce::Path track, val;
    track.addCentredArc(c.x, c.y, rad - thick, rad - thick, 0, a0, a1, true);
    val.addCentredArc(c.x, c.y, rad - thick, rad - thick, 0, a0, ang, true);
    g.setColour(col::grid);
    g.strokePath(track, juce::PathStrokeType(thick, juce::PathStrokeType::curved, juce::PathStrokeType::rounded));
    g.setColour(col::accent);
    g.strokePath(val, juce::PathStrokeType(thick, juce::PathStrokeType::curved, juce::PathStrokeType::rounded));
    g.setColour(col::panelHi);
    g.fillEllipse(r.reduced(thick * 2.2f));
    const juce::Point<float> tip(c.x + (rad - thick * 2.8f) * std::sin(ang), c.y - (rad - thick * 2.8f) * std::cos(ang));
    g.setColour(col::text);
    g.drawLine({c.getPointOnCircumference(rad * 0.15f, ang), tip}, 2.5f);
}

void LookAndFeel::drawToggleButton(juce::Graphics& g, juce::ToggleButton& b, bool hi, bool) {
    auto r = b.getLocalBounds().toFloat().reduced(1.f);
    const bool on = b.getToggleState();
    const auto onCol = b.getName() == "Bypass" ? col::cut : (b.getName() == "Lock all" ? col::fixed : col::accent);
    g.setColour(on ? onCol.withAlpha(0.22f) : (hi ? col::panelHi.brighter(0.08f) : col::panelHi));
    g.fillRoundedRectangle(r, 5.f);
    g.setColour(on ? onCol : col::grid);
    g.drawRoundedRectangle(r, 5.f, 1.2f);
    auto dot = r.removeFromLeft(r.getHeight()).reduced(r.getHeight() * 0.33f);
    g.setColour(on ? onCol : col::dim);
    g.fillEllipse(dot);
    g.setColour(on ? col::text : col::dim);
    g.setFont(juce::FontOptions(13.f, juce::Font::bold));
    g.drawText(b.getButtonText(), r.withTrimmedRight(4.f), juce::Justification::centred);
}

void LookAndFeel::drawButtonBackground(juce::Graphics& g, juce::Button& b, const juce::Colour&, bool hi, bool down) {
    auto r = b.getLocalBounds().toFloat().reduced(1.f);
    g.setColour(down ? col::accent.withAlpha(0.35f) : (hi ? col::panelHi.brighter(0.1f) : col::panelHi));
    g.fillRoundedRectangle(r, 5.f);
    g.setColour(col::grid);
    g.drawRoundedRectangle(r, 5.f, 1.2f);
}

// ---------------------------------------------------------------- meters
void MeterBar::tick(int channels) {
    nch = juce::jlimit(1, dfb::kMaxChannels, channels);
    for (int c = 0; c < nch; ++c) {
        peakDb[c] = toDb(meters[c].peak.load(std::memory_order_relaxed));
        rmsDb[c] = toDb(meters[c].rms.load(std::memory_order_relaxed) * 1.41421f);
        if (peakDb[c] >= holdDb[c] || ++holdAge[c] > 45) { holdDb[c] = peakDb[c]; holdAge[c] = 0; }
    }
    repaint();
}

void MeterBar::paint(juce::Graphics& g) {
    auto r = getLocalBounds().toFloat();
    g.setColour(col::panel);
    g.fillRoundedRectangle(r, 6.f);
    auto title = r.removeFromTop(20.f);
    g.setColour(col::dim);
    g.setFont(juce::FontOptions(11.f, juce::Font::bold));
    g.drawText(name, title, juce::Justification::centred);
    auto readout = r.removeFromBottom(18.f);
    r = r.reduced(6.f, 4.f);
    auto scale = r.removeFromLeft(22.f);
    const float lo = -60.f;
    auto yFor = [&](float db) { return r.getBottom() - r.getHeight() * juce::jlimit(0.f, 1.f, (db - lo) / -lo); };
    g.setFont(juce::FontOptions(9.f));
    for (float db : {0.f, -6.f, -12.f, -24.f, -36.f, -48.f}) {
        g.setColour(col::dim);
        g.drawText(juce::String((int)db), scale.withY(yFor(db) - 6.f).withHeight(12.f), juce::Justification::centredRight);
        g.setColour(col::grid.withAlpha(0.6f));
        g.drawHorizontalLine((int)yFor(db), r.getX(), r.getRight());
    }
    const float gap = 3.f, bw = (r.getWidth() - gap * (nch - 1)) / nch;
    juce::ColourGradient grad(col::meterHi, 0, yFor(0.f), col::meterLo, 0, yFor(-30.f), false);
    grad.addColour(0.35, col::meterMid);
    float maxPk = -120.f;
    for (int c = 0; c < nch; ++c) {
        auto bar = juce::Rectangle<float>(r.getX() + c * (bw + gap), r.getY(), bw, r.getHeight());
        g.setColour(col::bg);
        g.fillRoundedRectangle(bar, 2.f);
        g.setGradientFill(grad);
        g.setOpacity(0.45f);
        g.fillRect(bar.withTop(yFor(peakDb[c])));
        g.setOpacity(1.f);
        g.fillRect(bar.withTop(yFor(rmsDb[c])).reduced(bw * 0.18f, 0));
        g.setColour(holdDb[c] > -1.f ? col::cut : col::text);
        g.fillRect(bar.withY(yFor(holdDb[c]) - 1.f).withHeight(2.f));
        maxPk = juce::jmax(maxPk, holdDb[c]);
    }
    g.setColour(maxPk > -1.f ? col::cut : col::text);
    g.setFont(juce::FontOptions(11.f));
    g.drawText(maxPk <= -119.f ? "-inf" : juce::String(maxPk, 1), readout, juce::Justification::centred);
}

// ---------------------------------------------------------------- spectrum
float SpectrumView::xFor(double f) const {
    return (float)(std::log(f / 20.0) / std::log(1000.0)) * (float)getWidth();
}

void SpectrumView::paint(juce::Graphics& g) {
    const auto r = getLocalBounds().toFloat();
    g.setColour(col::panel);
    g.fillRoundedRectangle(r, 6.f);
    const float H = r.getHeight(), W = r.getWidth();
    const float topDb = 0.f, botDb = -100.f;
    auto yFor = [&](float db) { return H * (topDb - db) / (topDb - botDb); };
    // cut curve lives on its own scale in the lower part: 0 dB line at 62% height, -24 dB at the bottom
    const float cutZero = H * 0.62f;
    auto yCut = [&](double db) { return cutZero + (float)(-db / 24.0) * (H - cutZero - 4.f); };

    g.setFont(juce::FontOptions(10.f));
    for (double f : {50.0, 100.0, 200.0, 500.0, 1000.0, 2000.0, 5000.0, 10000.0}) {
        const float x = xFor(f);
        g.setColour(col::grid.withAlpha(0.7f));
        g.drawVerticalLine((int)x, 0.f, H);
        g.setColour(col::dim);
        g.drawText(f >= 1000 ? juce::String(f / 1000, 0) + "k" : juce::String((int)f), (int)x + 3, (int)H - 14, 40, 12, juce::Justification::left);
    }
    for (float db : {-20.f, -40.f, -60.f, -80.f}) {
        g.setColour(col::grid.withAlpha(0.35f));
        g.drawHorizontalLine((int)yFor(db), 0.f, W);
        g.setColour(col::dim);
        g.drawText(juce::String((int)db), 4, (int)yFor(db) - 12, 40, 12, juce::Justification::left);
    }

    // spectrum
    juce::Path spec;
    spec.startNewSubPath(0, H);
    for (int i = 0; i < dfb::Engine::kDisplayBins; ++i) {
        const float x = xFor(dfb::Engine::displayFreq(i));
        spec.lineTo(x, juce::jlimit(0.f, H, yFor(engine.display[i].load(std::memory_order_relaxed))));
    }
    spec.lineTo(W, H);
    spec.closeSubPath();
    g.setGradientFill(juce::ColourGradient(col::accent.withAlpha(0.45f), 0, 0, col::accent.withAlpha(0.05f), 0, H, false));
    g.fillPath(spec);
    g.setColour(col::accent.withAlpha(0.9f));
    g.strokePath(spec, juce::PathStrokeType(1.3f));

    // cut curve
    g.setColour(col::dim.withAlpha(0.5f));
    g.drawHorizontalLine((int)cutZero, 0.f, W);
    juce::Path cut;
    bool any = false;
    for (int i = 0; i < dfb::kMaxNotches; ++i) any |= engine.slotView[i].state.load() != 0;
    if (any) {
        // rebuild from the published slot values (GUI thread only reads atomics)
        dfb::NotchBank view;
        for (int i = 0; i < dfb::kMaxNotches; ++i) {
            auto& s = view.slots[i];
            s.active = engine.slotView[i].state.load() != 0;
            s.freq = engine.slotView[i].freq.load(); s.curDb = engine.slotView[i].db.load(); s.curOct = engine.slotView[i].oct.load();
        }
        for (int x = 0; x <= (int)W; x += 2) {
            const double f = 20.0 * std::pow(1000.0, x / W);
            const float y = yCut(juce::jlimit(-30.0, 0.0, view.responseDb(f)));
            if (x == 0) cut.startNewSubPath((float)x, y); else cut.lineTo((float)x, y);
        }
        g.setColour(col::cut.withAlpha(0.95f));
        g.strokePath(cut, juce::PathStrokeType(2.f));
    }
    g.setColour(col::dim);
    g.drawText("CUTS 0 dB", (int)W - 74, (int)cutZero - 13, 70, 12, juce::Justification::right);

    // markers
    for (int i = 0; i < dfb::kMaxNotches; ++i) {
        const int st = engine.slotView[i].state.load();
        if (!st) continue;
        const float f = engine.slotView[i].freq.load(), db = engine.slotView[i].db.load();
        const float x = xFor(f), y = yCut(db);
        const auto c = st == 2 ? col::fixed : col::cut;
        g.setColour(c);
        g.fillEllipse(x - 4.f, y - 4.f, 8.f, 8.f);
        g.setFont(juce::FontOptions(10.f, juce::Font::bold));
        const juce::String txt = (f >= 1000 ? juce::String(f / 1000.f, 2) + "k" : juce::String(juce::roundToInt(f))) + " " + juce::String(db, 1);
        g.drawText(txt, (int)x - 40, (int)y + 5, 80, 12, juce::Justification::centred);
    }
}

// ---------------------------------------------------------------- notch list
void NotchList::paint(juce::Graphics& g) {
    auto r = getLocalBounds().toFloat();
    const float gap = 4.f, w = (r.getWidth() - gap * (dfb::kMaxNotches - 1)) / dfb::kMaxNotches;
    for (int i = 0; i < dfb::kMaxNotches; ++i) {
        auto cell = juce::Rectangle<float>(r.getX() + i * (w + gap), r.getY(), w, r.getHeight());
        const int st = engine.slotView[i].state.load();
        const auto c = st == 2 ? col::fixed : (st == 1 ? col::cut : col::grid);
        g.setColour(st ? c.withAlpha(0.14f) : col::panel);
        g.fillRoundedRectangle(cell, 4.f);
        g.setColour(c.withAlpha(st ? 0.9f : 0.5f));
        g.drawRoundedRectangle(cell.reduced(0.5f), 4.f, 1.f);
        g.setFont(juce::FontOptions(10.f, juce::Font::bold));
        if (!st) { g.setColour(col::dim); g.drawText(juce::String(i + 1), cell, juce::Justification::centred); continue; }
        const float f = engine.slotView[i].freq.load(), db = engine.slotView[i].db.load();
        auto top = cell.removeFromTop(cell.getHeight() * 0.5f);
        g.setColour(col::text);
        g.drawText(f >= 1000 ? juce::String(f / 1000.f, 2) + "k" : juce::String(juce::roundToInt(f)), top, juce::Justification::centredBottom);
        g.setColour(c);
        g.drawText(juce::String(db, 1) + (st == 2 ? " F" : ""), cell, juce::Justification::centredTop);
    }
}

// ---------------------------------------------------------------- editor
DeFeedbackEditor::DeFeedbackEditor(DeFeedbackProcessor& p)
    : AudioProcessorEditor(&p), proc(p),
      inMeter(p.engine.inMeter, "IN"), outMeter(p.engine.outMeter, "OUT"),
      spectrum(p.engine), notches(p.engine) {
    setLookAndFeel(&lnf);

    addAndMakeVisible(inMeter);
    addAndMakeVisible(outMeter);
    addAndMakeVisible(spectrum);
    addAndMakeVisible(notches);
    inMeter.setTooltip("Level coming in from the desk channel (peak bar, RMS bar, peak hold).");
    outMeter.setTooltip("Level after the feedback cuts.");
    spectrum.setTooltip("Live spectrum of the output. The red line is the combined cut curve; dots are the notches (amber = fixed).");

    setupKnob(sens, "sens", "SENSITIVITY", "Higher catches rings sooner and quieter (about 35 ms at 100%, 220 ms at 0%), with more risk of cutting held notes.");
    setupKnob(depth, "depth", "MAX CUT", "The deepest any single notch is allowed to go.");
    setupKnob(width, "width", "WIDTH", "Width of each notch. Narrow keeps the tone; wider catches drifting rings.");
    setupKnob(hold, "hold", "HOLD", "How long a cut stays after the ring stops before it slowly fades out (1.5 dB/s). A ring that comes back 3 times becomes a fixed cut.");

    mode.addItemList({"Live", "Ring-out"}, 1);
    mode.setTooltip("Live: protects music, cuts release over time. Ring-out: for soundcheck, extra sensitive and every cut stays fixed.");
    addAndMakeVisible(mode);
    modeAtt = std::make_unique<ComboAtt>(p.apvts, "mode", mode);

    for (auto* b : {&lock, &freeze, &bypass}) { b->setName(b->getButtonText()); addAndMakeVisible(*b); }
    lock.setTooltip("Keep every cut (no release). Fixed cuts are saved with your session.");
    freeze.setTooltip("Stop adding new cuts; existing cuts stay.");
    bypass.setTooltip("Bypass all cuts (detection keeps running so you can see rings).");
    lockAtt = std::make_unique<ButtonAtt>(p.apvts, "lock", lock);
    freezeAtt = std::make_unique<ButtonAtt>(p.apvts, "freeze", freeze);
    bypassAtt = std::make_unique<ButtonAtt>(p.apvts, "bypass", bypass);

    clearAll.setTooltip("Remove every cut, including fixed ones (they fade out smoothly).");
    clearLive.setTooltip("Remove the live cuts, keep the fixed ones.");
    clearAll.onClick = [this] { proc.clearRequest = true; };
    clearLive.onClick = [this] { proc.clearLiveRequest = true; };
    addAndMakeVisible(clearAll);
    addAndMakeVisible(clearLive);

    sourceLabel.setText(p.sourceName, juce::dontSendNotification);
    sourceLabel.setEditable(true);
    sourceLabel.setTooltip("Name the desk channel this is on (click to edit), e.g. Qu-16 Ch 3 Vocal.");
    sourceLabel.setColour(juce::Label::backgroundColourId, col::panelHi);
    sourceLabel.setColour(juce::Label::outlineColourId, col::grid);
    sourceLabel.setFont(juce::FontOptions(14.f, juce::Font::bold));
    sourceLabel.onTextChange = [this] { proc.sourceName = sourceLabel.getText(); };
    addAndMakeVisible(sourceLabel);

    status.setFont(juce::FontOptions(12.f));
    status.setColour(juce::Label::textColourId, col::dim);
    addAndMakeVisible(status);
    modelInfo.setFont(juce::FontOptions(10.f));
    modelInfo.setColour(juce::Label::textColourId, col::dim);
    modelInfo.setJustificationType(juce::Justification::centredRight);
    modelInfo.setText(juce::String("AI classifier: ") + dfb::model::kInfo + "  |  zero latency", juce::dontSendNotification);
    addAndMakeVisible(modelInfo);

    setResizable(true, true);
    setResizeLimits(760, 480, 1800, 1100);
    setSize(960, 600);
    startTimerHz(30);
}

DeFeedbackEditor::~DeFeedbackEditor() { setLookAndFeel(nullptr); }

void DeFeedbackEditor::setupKnob(Knob& k, const juce::String& id, const juce::String& title, const juce::String& tip) {
    k.slider.setTextBoxStyle(juce::Slider::TextBoxBelow, false, 80, 18);
    k.slider.setTooltip(tip);
    k.slider.setColour(juce::Slider::textBoxOutlineColourId, juce::Colours::transparentBlack);
    k.slider.setColour(juce::Slider::textBoxBackgroundColourId, col::panelHi);
    k.slider.setColour(juce::Slider::textBoxTextColourId, col::text);
    k.label.setText(title, juce::dontSendNotification);
    k.label.setJustificationType(juce::Justification::centred);
    k.label.setFont(juce::FontOptions(11.f, juce::Font::bold));
    k.label.setColour(juce::Label::textColourId, col::dim);
    addAndMakeVisible(k.slider);
    addAndMakeVisible(k.label);
    k.att = std::make_unique<SliderAtt>(proc.apvts, id, k.slider);
}

void DeFeedbackEditor::paint(juce::Graphics& g) {
    g.fillAll(col::bg);
    auto top = getLocalBounds().removeFromTop(48).toFloat();
    g.setColour(col::panel);
    g.fillRect(top);
    g.setColour(col::accent);
    g.setFont(juce::FontOptions(20.f, juce::Font::bold));
    g.drawText("DE-FEEDBACK", 16, 0, 140, 48, juce::Justification::centredLeft);
    g.setColour(col::text);
    g.drawText("AI", 152, 0, 40, 48, juce::Justification::centredLeft);
    // activity lamp
    const bool flash = juce::Time::getMillisecondCounterHiRes() < flashUntil;
    g.setColour(flash ? col::cut : col::grid);
    g.fillEllipse(188.f, 18.f, 12.f, 12.f);
    // knob panel
    auto bottom = getLocalBounds().removeFromBottom(150).reduced(10, 6).toFloat();
    g.setColour(col::panel);
    g.fillRoundedRectangle(bottom, 6.f);
}

void DeFeedbackEditor::resized() {
    auto r = getLocalBounds();
    auto top = r.removeFromTop(48).reduced(10, 9);
    top.removeFromLeft(200);
    sourceLabel.setBounds(top.removeFromLeft(180));
    top.removeFromLeft(10);
    mode.setBounds(top.removeFromLeft(110));
    bypass.setBounds(top.removeFromRight(90));
    top.removeFromRight(6);
    freeze.setBounds(top.removeFromRight(86));
    top.removeFromRight(6);
    lock.setBounds(top.removeFromRight(96));

    auto bottom = r.removeFromBottom(150).reduced(10, 6).reduced(8, 6);
    auto buttons = bottom.removeFromRight(120);
    clearAll.setBounds(buttons.removeFromTop(34).reduced(0, 2));
    buttons.removeFromTop(6);
    clearLive.setBounds(buttons.removeFromTop(34).reduced(0, 2));
    buttons.removeFromTop(6);
    status.setBounds(buttons.removeFromTop(34));
    const int kw = bottom.getWidth() / 4;
    for (auto* k : {&sens, &depth, &width, &hold}) {
        auto cell = bottom.removeFromLeft(kw);
        k->label.setBounds(cell.removeFromTop(16));
        k->slider.setBounds(cell);
    }

    auto mid = r.reduced(10, 4);
    modelInfo.setBounds(mid.removeFromBottom(14));
    notches.setBounds(mid.removeFromBottom(40));
    mid.removeFromBottom(6);
    inMeter.setBounds(mid.removeFromLeft(70));
    outMeter.setBounds(mid.removeFromRight(70));
    spectrum.setBounds(mid.reduced(8, 0));
}

void DeFeedbackEditor::timerCallback() {
    const int ch = proc.engine.numChannels.load();
    inMeter.tick(ch);
    outMeter.tick(ch);
    spectrum.tick();
    notches.repaint();
    const int ev = proc.engine.eventCount.load();
    if (ev != lastEvents) {
        lastEvents = ev;
        flashUntil = juce::Time::getMillisecondCounterHiRes() + 250.0;
        const float f = proc.engine.lastEventFreq.load();
        status.setText("Ring at " + (f >= 1000 ? juce::String(f / 1000.f, 2) + " kHz" : juce::String(juce::roundToInt(f)) + " Hz"), juce::dontSendNotification);
        repaint(0, 0, 220, 48);
    } else if (juce::Time::getMillisecondCounterHiRes() > flashUntil + 50 && flashUntil > 0) {
        flashUntil = 0;
        repaint(0, 0, 220, 48);
    }
}
