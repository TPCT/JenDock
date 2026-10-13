// expect: line 2: outside pipeline
import groovy.json.JsonSlurper
pipeline {
    agent any
    stages { stage('A') { steps { echo 'x' } } }
}
