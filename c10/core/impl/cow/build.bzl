def define_targets(rules):
    rules.cc_library(
        name = "simulator",
        srcs = ["simulator.cpp"],
        hdrs = ["simulator.h"],
        deps = [
            "//c10/macros",
            "//c10/util:base",
        ],
        visibility = ["//c10/test:__pkg__"],
    )

    rules.cc_library(
        name = "spy",
        srcs = ["spy.cpp"],
        hdrs = ["spy.h"],
        deps = [
            "//c10/macros",
            "//c10/util:base",
        ],
    )

    rules.cc_library(
        name = "state",
        srcs = ["state.cpp"],
        hdrs = ["state.h"],
        deps = [
            ":simulator",
            "//c10/util:base",
        ],
        visibility = ["//c10/test:__pkg__"],
    )
