cmake_minimum_required(VERSION 3.22)
project(DeFeedbackAI VERSION 1.0.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
add_compile_definitions(_USE_MATH_DEFINES NOMINMAX)
if(MSVC)
  set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
endif()

option(DFB_BUILD_PLUGIN "Build the JUCE plugin" ON)
option(DFB_BUILD_TOOLS "Build the simulator and tests" ON)

if(DFB_BUILD_TOOLS)
  add_executable(dfb_sim tools/dfb_sim.cpp)
  add_executable(dfb_tests tests/test_core.cpp)
  enable_testing()
  add_test(NAME core COMMAND dfb_tests)
endif()

if(DFB_BUILD_PLUGIN)
  if(DEFINED JUCE_DIR)
    add_subdirectory(${JUCE_DIR} juce)
  else()
    include(FetchContent)
    FetchContent_Declare(juce GIT_REPOSITORY https://github.com/juce-framework/JUCE.git GIT_TAG 8.0.9 GIT_SHALLOW ON)
    FetchContent_MakeAvailable(juce)
  endif()

  juce_add_plugin(DeFeedbackAI
    COMPANY_NAME "REA Apps"
    BUNDLE_ID com.reaapps.defeedbackai
    PLUGIN_MANUFACTURER_CODE Reaa
    PLUGIN_CODE Dfai
    FORMATS VST3 Standalone
    PRODUCT_NAME "De-Feedback AI"
    IS_SYNTH FALSE
    NEEDS_MIDI_INPUT FALSE
    NEEDS_MIDI_OUTPUT FALSE
    IS_MIDI_EFFECT FALSE
    EDITOR_WANTS_KEYBOARD_FOCUS FALSE
    COPY_PLUGIN_AFTER_BUILD FALSE
    VST3_CATEGORIES Fx EQ Restoration)

  target_sources(DeFeedbackAI PRIVATE source/PluginProcessor.cpp source/PluginEditor.cpp)
  target_include_directories(DeFeedbackAI PRIVATE core)
  target_compile_definitions(DeFeedbackAI PUBLIC
    JUCE_WEB_BROWSER=0 JUCE_USE_CURL=0 JUCE_VST3_CAN_REPLACE_VST2=0
    JUCE_DISPLAY_SPLASH_SCREEN=0 _USE_MATH_DEFINES)
  target_link_libraries(DeFeedbackAI PRIVATE
    juce::juce_audio_utils juce::juce_dsp
    PUBLIC juce::juce_recommended_config_flags juce::juce_recommended_lto_flags juce::juce_recommended_warning_flags)

  # UI snapshot tool (renders the editor to a PNG without a host)
  juce_add_console_app(dfb_snapshot PRODUCT_NAME "dfb_snapshot")
  target_sources(dfb_snapshot PRIVATE tools/snapshot.cpp source/PluginProcessor.cpp source/PluginEditor.cpp)
  target_include_directories(dfb_snapshot PRIVATE core)
  target_compile_definitions(dfb_snapshot PRIVATE JUCE_WEB_BROWSER=0 JUCE_USE_CURL=0 _USE_MATH_DEFINES
    JucePlugin_Name="De-Feedback AI" JUCE_STANDALONE_APPLICATION=1)
  target_link_libraries(dfb_snapshot PRIVATE juce::juce_audio_utils juce::juce_dsp juce::juce_recommended_config_flags)
endif()
