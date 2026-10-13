// expect: line 6: code after the pipeline
pipeline {
    agent any
    stages { stage('A') { steps { echo 'x' } } }
}
echo 'trailing'
