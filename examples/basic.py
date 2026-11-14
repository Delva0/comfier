"""Run eager, graph, intermediate and native-JSON calls against a configured daemon."""

import argparse
import asyncio
import json
import os

from comani import Client


async def run(url, token):
    async with Client(url, token=token) as remote:
        image = remote.node("EmptyImage", width=64, height=64, batch_size=1, color=0x123456)
        adjusted = remote.python(lambda image: image * 0.5, inputs={"image": image.output()})
        inverted = remote.node("ImageInvert", image=adjusted.output())
        result = await inverted.output()
        local = await result.fetch()
        with remote.graph() as workflow:
            image_in = workflow.input("image", type="IMAGE")
            intermediate, = await remote.node("ImageInvert", image=image_in)
            restored = remote.node("ImageInvert", image=intermediate)
            workflow.output("image", restored.output())
        submitted = remote.submit(workflow, inputs={"image": adjusted.output()})
        middle = await submitted[intermediate]
        final = await submitted.output("image")
        final_value = await final.fetch()
        prompt = {"image": {"class_type": "EmptyImage", "inputs": {
            "width": 64, "height": 64, "batch_size": 1, "color": 0,
        }}}
        native = remote.submit(prompt, outputs={"image": ["image", 0]}, targets=["image"])
        native_value = await (await native.output("image")).fetch()
        print(json.dumps({"eager_shape": list(local.shape),
                          "workflow_shape": list(final_value.shape),
                          "intermediate_ownership": middle.ownership,
                          "native_json_shape": list(native_value.shape)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8190")
    parser.add_argument("--token", default=os.environ.get("COMANI_TOKEN"))
    args = parser.parse_args()
    asyncio.run(run(args.url, args.token))


if __name__ == "__main__":
    main()
